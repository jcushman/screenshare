import base64
import hashlib
import hmac
import json
import logging

import requests
from django.conf import settings
from django.core.exceptions import SuspiciousOperation
from django.http import HttpResponse
from django.utils.encoding import force_bytes, force_str
from django.views.decorators.csrf import csrf_exempt
from main.helpers import get_message_history, message_for_ts, send_state

logger = logging.getLogger(__name__)


### helpers ###

def verify_slack_request(request):
    """ Raise SuspiciousOperation if request was not signed by Slack. """
    basestring = b":".join([
        b"v0",
        force_bytes(request.META.get("HTTP_X_SLACK_REQUEST_TIMESTAMP", b"")),
        request.body
    ])
    expected_signature = 'v0=' + hmac.new(settings.SLACK['signing_secret'], basestring, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected_signature, force_str(request.META.get("HTTP_X_SLACK_SIGNATURE", ""))):
        raise SuspiciousOperation("Slack signature verification failed")

colors = ['black', 'red', 'orange', 'yellow', 'green', 'blue', 'purple']
def handle_reactions(message, is_most_recent):
    old_color = message['color']
    new_color = None
    for reaction in message['reactions']:
        if 'night' in reaction:
            new_color = "#000"
        else:
            new_color = next((
                color
                for color in colors
                if color in reaction
            ), None)
        if new_color:
            break
    if old_color != new_color:
        message["color"] = new_color or '#fff'
        if is_most_recent:
            send_state({"color": message["color"]})

def store_image(event, file_response):
    """ Add requested file to message_history """
    encoded_image = "data:%s;base64,%s" % (
        file_response.headers['Content-Type'],
        base64.b64encode(file_response.content).decode())
    with get_message_history() as message_history:
        message_history.append({
            "id": event['ts'],
            "image": encoded_image,
            "reactions": [],
            "color": "#fff",
        })
        send_state(message_history[-1])

### views ###

@csrf_exempt
def slack_event(request):
    """ Handle message from Slack. """
    verify_slack_request(request)
    event = json.loads(request.body.decode("utf-8"))
    logger.info(event)

    # url verification
    if event["type"] == "url_verification":
        return HttpResponse(event["challenge"], content_type='text/plain')

    event = event["event"]

    # message in channel
    if event["type"] == "message":

        message_type = event.get("subtype")

        # handle uploaded image
        if message_type == "file_share":
            # {
            #   'type': 'message',
            #   'files': [{
            #       'filetype': 'png',
            #       'url_private': 'https://files.slack.com/files-pri/T02RW19TT-FBY895N1Z/image.png'
            #   }],
            #   'ts': '1532713362.000505',
            #   'subtype': 'file_share',
            # }
            file_info = event["files"][0]
            if file_info["filetype"] in ("jpg", "gif", "png"):
                # if image, fetch file and send to listeners
                file_response = requests.get(file_info["url_private"], headers={"Authorization": "Bearer %s" % settings.SLACK["bot_access_token"]})
                if file_response.headers['Content-Type'].startswith('text/html'):
                    logger.error("Failed to fetch image; check bot_access_token")
                else:
                    store_image(event, file_response)

        # handle image URL
        elif event.get('message') and event['message'].get('attachments') and event['message']['attachments'][0].get('image_url'):
            # what we get when slack unfurls an image URL -- a nested message:
            # {
            #   'type': 'message',
            #   'subtype': 'message_changed',
            #   'message': {
            #       'attachments': [{
            #           'image_url': 'some external url'
            #       }],
            #      'ts': '1532713362.000505',
            #   },
            # }
            try:
                file_response = requests.get(event['message']['attachments'][0]['image_url'])
                assert file_response.ok
                assert any(file_response.headers['Content-Type'].startswith(prefix) for prefix in ('image/jpeg', 'image/gif', 'image/png'))
            except (requests.RequestException, AssertionError) as e:
                logger.error("Failed to fetch URL: %s" % e)
            else:
                store_image(event['message'], file_response)

        # handle message deleted
        elif message_type == "message_deleted" and event.get('previous_message'):
            with get_message_history() as message_history:
                message, is_most_recent = message_for_ts(message_history, event['previous_message']['ts'])
                if message:
                    message_history.pop()
                    if is_most_recent and message_history:
                        send_state(message_history[-1])

    # handle reactions
    elif event["type"] == "reaction_added":
        # {
        #   'type': 'reaction_added',
        #   'user': 'U02RXC5JN',
        #   'item': {'type': 'message', 'channel': 'CBU9W589K', 'ts': '1532713362.000505'},
        #   'reaction': 'rage',
        #   'item_user': 'U02RXC5JN',
        #   'event_ts': '1532713400.000429'
        # }
        with get_message_history() as message_history:
            message, is_most_recent = message_for_ts(message_history, event['item']['ts'])
            if message:
                message['reactions'].insert(0, event["reaction"])
                handle_reactions(message, is_most_recent)

    elif event["type"] == "reaction_removed":
        with get_message_history() as message_history:
            message, is_most_recent = message_for_ts(message_history, event['item']['ts'])
            if message:
                try:
                    message['reactions'].remove(event["reaction"])
                except ValueError:
                    pass
                else:
                    handle_reactions(message, is_most_recent)

    # tell Slack not to resend
    return HttpResponse()
