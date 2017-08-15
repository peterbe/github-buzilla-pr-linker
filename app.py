import base64
import json
import os
import sys
import re
import hmac
import logging
# import ipaddress
# from pprint import pprint

import requests
from requests.adapters import HTTPAdapter
from requests.packages.urllib3.util.retry import Retry
from decouple import config
from flask import Flask, request, abort


DEBUG = config('DEBUG', False, cast=bool)
GITHUB_WEBHOOK_SECRET = config('GITHUB_WEBHOOK_SECRET', 'secret')
BUGZILLA_BASE_URL = config('BUGZILLA_BASE_URL', 'https://bugzilla.mozilla.org')
# To generate one, go to https://bugzilla.mozilla.org/userprefs.cgi?tab=apikey
# For production grade you probably want this to be tied to a more "formal"
# user. Aka. some bot account.
# XXX perhaps we can use this:
# https://mana.mozilla.org/wiki/display/WebDev/Bugzilla+Github+Bug+Closer+Account
BUGZILLA_API_KEY = config('BUGZILLA_API_KEY')

# Optional
GHE_ADDRESS = config('GHE_ADDRESS', None)


app = Flask(__name__)
if 'DYNO' in os.environ:
    app.logger.addHandler(logging.StreamHandler(sys.stdout))
    app.logger.setLevel(
        logging.DEBUG if DEBUG else logging.INFO
    )


def requests_retry_session(
    retries=3,
    backoff_factor=0.3,
    status_forcelist=(502, 504),
    session=None,
):
    session = session or requests.Session()
    retry = Retry(
        total=retries,
        read=retries,
        connect=retries,
        backoff_factor=backoff_factor,
        status_forcelist=status_forcelist,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount('http://', adapter)
    session.mount('https://', adapter)
    return session



class ConfigurationError(ValueError):
    """when there's something wrong with the current config values"""


@app.route('/postreceive', methods=['POST', 'GET'])
def postreceive():
    logger = app.logger

    if request.method == 'GET':
        return "Yeah, it works but use POST\n"

    # app.logger.debug('APP DEBUG')
    # app.logger.info('APP INFO')
    # app.logger.warning('APP WARNING')
    # app.logger.error('APP ERROR')

    # # Store the IP address of the requester
    # print('request.remote_addr', repr(request.remote_addr))
    # request_ip = ipaddress.ip_address(request.remote_addr)
    #
    # # If GHE_ADDRESS is specified, use it as the hook_blocks.
    # if GHE_ADDRESS:
    #     hook_blocks = [GHE_ADDRESS]
    # # Otherwise get the hook address blocks from the API.
    # else:
    #     # XXX cache this
    #     hook_blocks = requests.get(
    #         'https://api.github.com/meta'
    #     ).json()['hooks']
    #
    # # Check if the POST request is from github.com or GHE
    # print('hook_blocks', hook_blocks)
    # print('request_ip', repr(request_ip))
    # for block in hook_blocks:
    #     if ipaddress.ip_address(request_ip) in ipaddress.ip_network(block):
    #         break  # the remote_addr is within the network range of github.
    # else:
    #     if request_ip != '127.0.0.1':
    #         print('request_ip != 127.0.0.1')
    #         abort(403)

    if request.headers.get('X-GitHub-Event') == 'ping':
        return {'msg': 'Hi!'}

    if GITHUB_WEBHOOK_SECRET == 'secret' and not DEBUG:
        raise ConfigurationError(
            'GITHUB_WEBHOOK_SECRET not set'
        )

    # Need do a SHA check on the payload
    header_signature = request.headers.get('X-Hub-Signature')
    if header_signature is None:
        logger.warning(
            'No X-Hub-Signature header in request'
        )
        abort(403)

    sha_name, signature = header_signature.split('=')
    if sha_name != 'sha1':
        logger.warning(f'Algo used expected to be sha1, not {sha_name!r}')
        abort(400)

    raw_payload = request.get_data()
    logger.debug(f'raw_payload is {len(raw_payload)} bytes')
    # print('raw_payload', len(raw_payload), repr(raw_payload[:50]))
    # print('request.data:')
    # print(type(request.get_data()))
    # print(repr(request.get_data()))
    # print('request.body:')
    # print(type(request.body))
    # print(repr(request.body))

    # HMAC requires the key to be bytes, but data is string
    # print('request.data', len(request.data))
    # print('request.get_data()', len(request.get_data()))
    mac = hmac.new(
        GITHUB_WEBHOOK_SECRET.encode('utf-8'),
        msg=raw_payload,
        digestmod='sha1'
    )

    if mac.hexdigest() != signature:
        logger.warning('HMAC signature did not match')
        abort(403)

    posted = json.loads(request.form['payload'])
    # posted=payload
    # from pprint import pprint
    # logger.info(type(posted))
    # logger.info(str(posted)[:1000])
    # pprint(posted)

    if not posted.get('pull_request'):
        logger.warning(
            'Not a pull_request {!r}'.format(posted.get('pull_request'))
        )
        abort(400)

    if posted.get('action') != 'opened':  # only created PRs
        logger.warning("Action was NOT 'opened'. It was {!r}".format(
            posted.get('action')
        ))
        return 'OK'

    pull_request = posted['pull_request']

    if not pull_request.get('title') or not find_bug_id(pull_request['title']):
        logger.info('No bug ID found in title {!r}'.format(
            pull_request.get('title')
        ))
        return 'No bug ID found in the title'

    session = requests_retry_session()

    url = pull_request['_links']['html']['href']
    bug_id = find_bug_id(pull_request['title'])
    # Can we find the bug at all?!
    bug_comments = find_bug_comments(session, bug_id)
    # Note, if the bug doesn't have any comments 'bug_comments' will
    # be an empty list, not None.
    if bug_comments is None:
        # Oh no! Bug can't be found
        logger.warning(f'Bug {bug_id!r} can not be found')
        abort(400)

    # print("BUG_COMMENTS")
    # print(repr(bug_comments))

    # loop over the current comments to see if there's already on
    for i, comment in enumerate(bug_comments):
        # print('COMMENT')
        # print(comment)
        if comment.get('is_obsolete'):
            continue
        if url in comment['text']:
            # exit early!
            logger.info(f'Pull request URL already in comment {i+1}')
            pass#return

    # let's go ahead and post the comment!
    attachment_url = f'{BUGZILLA_BASE_URL}/rest/bug/{bug_id}/attachment'
    summary = f'Link to GitHub pull-request: {url}'
    pull_request_id = pull_request['id']

    diff_data = _get_diff_data(session, pull_request['diff_url']) or ''

    comment = pull_request.get('description', '*no pull request description*')

    response = requests.post(attachment_url, json={
        'ids': [bug_id],
        'summary': summary,
        'data': base64.b64encode(diff_data.encode('utf-8')).decode('utf-8'),
        'file_name': f'file_{pull_request_id}.txt',
        # 'content_type': 'text/x-github-pull-request',
        # 'content_type': 'text/plain',
        'comment': comment,
        'is_patch': 'true',
    }, headers={
        'X-BUGZILLA-API-KEY': BUGZILLA_API_KEY,
    })
    # print((response.status_code, response.content[:1000]))
    if response.status_code == 401:
        logger.error(
            'Unauthorized attempt to post attachment (%r)',
            response.content
        )
        abort(500)

    if response.status_code == 201:
        attachment_id = list(response.json()['attachments'].keys())[0]
        logger.info(f'Successfully posted attachment {attachment_id}')
        return "OK", 201
    else:
        logger.error('Unable to create attachment. {} ({!r})'.format(
            response.status_code,
            response.content
        ))
        return "OK"


def _get_diff_data(session, url):
    response = session.get(url)
    if response.status_code == 200:
        return response.text


def find_bug_comments(session, id):
    """Return true if the bug can be found"""
    # XXX should this use secure credentials??
    # bug_url = f'{BASE_URL}/rest/bug/{bug_id}/comment'
    # XXX Idea; it could
    bug_url = f'{BUGZILLA_BASE_URL}/rest/bug/{id}/comment'
    response = session.get(bug_url)
    print('bug_url', bug_url, response.status_code)
    if response.status_code == 200:
        return response.json()['bugs'][id]['comments']


def find_bug_id(text):
    """give a piece of text, that is presumed to be the pull request title,
    return the bug ID out of it, if found."""

    # XXX Is this right?
    # How does the bugcloser do it?
    regex = re.compile('bug (\d+)', re.I)
    for match in regex.findall(text):
        return match


@app.route('/')
def homepage():
    return "See README\n"


if __name__ == '__main__':
    app.run(debug=DEBUG, use_reloader=DEBUG)
