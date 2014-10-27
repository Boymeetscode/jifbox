import datetime
import os

try:
    from urlparse import urlparse       # python 2.x
except:
    from urllib.parse import urlparse   # python 3.x

from dropbox.client import DropboxClient, DropboxOAuth2Flow
from flask import Flask, abort, flash, jsonify, redirect, render_template, request, session, url_for
from pymongo import MongoClient
from rauth import OAuth1Service


#
# MongoDB configuration
#

mongo_url = os.environ.get('MONGOHQ_URL', 'mongodb://localhost:27017/jifbox')
mongo_conn = MongoClient(mongo_url)

mongo_params = urlparse(mongo_url)

if mongo_params.username and mongo_params.password:
    mongo_conn.authenticate(mongo_params.username, mongo_params.password)

mongo = mongo_conn[mongo_params.path.strip('/')]


#
# services
#

class Service(object):

    def __init__(self):
        self.register()

    def register(self):
        self._config = mongo.services.find_one({'service_id': self.service_id})
        if not self._config:
            doc = {
                'service_id': self.service_id,
            }
            doc_id = mongo.services.save(doc)
            self._config = mongo.services.find_one(doc_id)

    def __delitem__(self, key):
        if key in self._config:
            del self._config[key]
            mongo.services.save(self._config)

    def __getitem__(self, key):
        return self._config.get(key)

    def __setitem__(self, key, value):
        self._config[key] = value
        mongo.services.save(self._config)

    def process(self, payload):
        raise NotImplementedError('process must be implemented by services')


class DropboxService(Service):

    service_id = 'dropbox'

    def __init__(self):
        super(DropboxService, self).__init__()
        self.client_key = os.environ.get('DROPBOX_KEY')
        self.client_secret = os.environ.get('DROPBOX_SECRET')

        access_token = os.environ.get('DROPBOX_TOKEN')
        if access_token:
            self['access_token'] = access_token

    @property
    def is_available(self):
        return bool(self.client_key and self.client_secret)

    @property
    def is_enabled(self):
        return bool(self['access_token'])

    def process(self, payload):
        client = DropboxClient(self['access_token'])
        meta = client.put_file(payload['filename'], payload['data'], overwrite=False)
        return meta


class TumblrService(Service):

    service_id = 'tumblr'

    def __init__(self):
        super(TumblrService, self).__init__()
        self.client_key = os.environ.get('TUMBLR_KEY')
        self.client_secret = os.environ.get('TUMBLR_SECRET')
        self.hostname = os.environ.get('TUMBLR_HOSTNAME')

    @property
    def is_available(self):
        return bool(self.client_key and self.client_secret and self.hostname)

    @property
    def is_enabled(self):
        return bool(self['access_token'])

    def process(self, payload):

        flow = tumblr_auth_flow()
        client = flow.get_session(self['access_token'])

        data = {
            'type': 'text',
            'state': 'published',
            'title': payload['filename'],
            'body': payload['data'],
        }

        meta = client.post('post', data=data).json()
        return meta


# pseudoservice for storing config info
class JIFBOXService(Service):
    service_id = 'jifbox'
    is_available = False
    is_enabled = False


services = {
    'dropbox': DropboxService(),
    'tumblr': TumblrService(),
}


#
# Dropbox stuff
#

def dropbox_auth_flow():
    dropbox = services['dropbox']
    redirect_uri = url_for('dropbox_callback', _external=True)
    flow = DropboxOAuth2Flow(dropbox.client_key, dropbox.client_secret,
        redirect_uri, session, 'dropbox-auth-csrf-token')
    return flow


#
# Tumblr stuff
#

def tumblr_auth_flow(request_token=None, request_secret=None):
    ts = services['tumblr']
    flow = OAuth1Service(
        name='tumblr',
        consumer_key=ts.client_key,
        consumer_secret=ts.client_secret,
        request_token_url='http://www.tumblr.com/oauth/request_token',
        access_token_url='http://www.tumblr.com/oauth/access_token',
        authorize_url='http://www.tumblr.com/oauth/authorize',
        base_url='https://api.tumblr.com/v2/blog/%s/' % ts.hostname)
    return flow


#
# the app
#

app = Flask(__name__)


@app.route('/')
def index():
    return render_template('jifbox.html')


@app.route('/giffed', methods=['POST'])
def giffed():

    response = {'active_services': {}}

    timestamp = datetime.datetime.utcnow()

    payload = {
        'filename': '%s.txt' % timestamp.isoformat(),
        'data': timestamp.isoformat(),
        'timestamp': timestamp,
    }

    for sid, service in services.items():
        if service.is_available and service.is_enabled:
            meta = service.process(payload)
            if meta:
                response['active_services'][sid] = meta

    return jsonify(response)


@app.route('/settings')
def settings():
    context = {'services': services}
    return render_template('settings.html', **context)


# Dropbox

@app.route('/settings/dropbox/auth')
def dropbox_auth():
    flow = dropbox_auth_flow()
    return redirect(flow.start())


@app.route('/settings/dropbox/callback')
def dropbox_callback():

    try:
        access_token, user_id, url_state = get_auth_flow().finish(request.args)
    except DropboxOAuth2Flow.BadRequestException, e:
        abort(400)
    except DropboxOAuth2Flow.BadStateException, e:
        abort(400)
    except DropboxOAuth2Flow.CsrfException, e:
        abort(403)
    except DropboxOAuth2Flow.NotApprovedException, e:
        flash('Not approved?  Why not')
        return redirect(url_for('home'))
    except DropboxOAuth2Flow.ProviderException, e:
        app.logger.exception("Auth error" + e)
        abort(403)

    services['dropbox']['access_token'] = access_token

    return redirect(url_for('settings'))


@app.route('/settings/dropbox/logout')
def dropbox_logout():
    del services['dropbox']['access_token']
    return redirect(url_for('settings'))


# Tumblr

@app.route('/settings/tumblr/auth')
def tumblr_auth():

    service = services['tumblr']
    flow = tumblr_auth_flow()

    request_token, request_token_secret = flow.get_request_token()
    authorize_url = flow.get_authorize_url(request_token)

    service['request_token'] = request_token
    service['request_secret'] = request_token_secret

    return redirect(authorize_url)


@app.route('/settings/tumblr/callback')
def tumblr_callback():

    oauth_token = request.args.get('oauth_token')
    oauth_verifier = request.args.get('oauth_verifier')

    if oauth_token and oauth_verifier:

        service = services['tumblr']

        flow = tumblr_auth_flow()
        access_token = flow.get_access_token(
            service['request_token'],
            service['request_secret'],
            method='GET',
            data={
                'oauth_verifier': oauth_verifier,
            })

        service['access_token'] = list(access_token)

        del services['tumblr']['request_token']
        del services['tumblr']['request_secret']

    return redirect(url_for('settings'))


@app.route('/settings/tumblr/logout')
def tumblr_logout():
    del services['tumblr']['access_token']
    return redirect(url_for('settings'))


#
# GET IT GIFFED!!!!!!!
#

if __name__ == '__main__':
    app.run(debug=True, port=8000)
