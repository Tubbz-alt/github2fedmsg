from pyramid.view import view_config
from pyramid.security import authenticated_userid
from pyramid.httpexceptions import HTTPFound

import pep8bot.models as m
from sqlalchemy import and_

import datetime
from hashlib import md5
import requests

import json
import retask.task
import retask.queue

import pep8bot.githubutils as gh

# http://developer.github.com/v3/repos/hooks/
github_api_url = "https://api.github.com/hub"
github_events = [
    "push",
    #"issues",
    #"issue_comment",
    "pull_request",
    #"gollum",
    #"watch",
    #"download",
    #"fork",
    #"fork_apply",
    #"member",
    #"public",
    #"status",
]


@view_config(route_name='home', renderer='index.mak')
def home(request):
    return {
    }


@view_config(route_name='webhook', request_method="POST", renderer='string')
def webhook(request):
    """ Handle github webhook. """

    # TODO -- check X-Hub-Signature
    # TODO -- make this secret
    salt = "TODO MAKE THIS SECRET"

    if 'payload' in request.params:
        payload = request.params['payload']
        if isinstance(payload, basestring):
            payload = json.loads(payload)

        if 'action' not in payload:
            # This is a regular old push.. don't worry about it.
            commits = [c['id'] for c in payload['commits']]
            username = payload['repository']['owner']['name']
            reponame = payload['repository']['name']
            clone_url = "https://github.com/%s/%s/" % (username, reponame)
        else:
            # This is a pull request
            sha = payload['pull_request']['head']['sha']
            commits = [sha]
            username = payload['repository']['owner']['login']
            reponame = payload['repository']['name']
            clone_url = payload['pull_request']['base']['repo']['clone_url']

        # Drop a note in our db about it
        user = m.User.query.filter_by(username=username).one()
        repo = m.Repo.query.filter(and_(
            m.Repo.name == reponame, m.Repo.username == username)).one()

        template = "https://github.com/%s/%s/commit/%s"

        for sha in commits:
            if m.Commit.query.filter_by(sha=sha).count() > 0:
                continue

            m.DBSession.add(m.Commit(
                status="pending",
                sha=sha,
                url=template % (username, reponame, sha),
                repo=repo,
            ))

            status = "pending"
            token = user.oauth_access_token
            desc = "PEP8Bot scan pending"
            gh.post_status(username, reponame, sha, status, token, desc)

        # Now, put a note in our work queue for it, too.
        queue = retask.queue.Queue('commits')
        task = retask.task.Task({
            'reponame': reponame,
            'username': username,
            'commits': commits,
            'clone_url': clone_url,
        })
        queue.connect()

        # Fire and forget
        job = queue.enqueue(task)

    else:
        raise NotImplementedError()

    return "OK"


@view_config(name='sync', context=m.User, renderer='json')
def sync_user(request):
    request.context.sync_repos()
    raise HTTPFound('/' + request.context.username)


@view_config(name='toggle', context=m.Repo, renderer='json')
def repo_toggle_enabled(request):
    possible_kinds = ['pep8', 'pylint', 'pyflakes', 'mccabe']
    possible_attrs = ['%s_enabled' % kind for kind in possible_kinds]

    kind = request.GET.get('kind')
    if kind not in possible_kinds:
        raise ValueError("%r not in %r" % (kind, possible_kinds))

    attr = '%s_enabled' % kind

    repo = request.context

    should_notify_github = False
    # If we had *no* attributes on *before* toggling, then *subscribe*.
    if not any([getattr(repo, a) for a in possible_attrs]):
        should_notify_github = True

    # Toggle that attribute on our db model.
    setattr(repo, attr, not getattr(repo, attr))

    # If we had *no* attributes on *after* toggling, then *unsubscribe*.
    if not any([getattr(repo, a) for a in possible_attrs]):
        should_notify_github = True

    if should_notify_github:
        token = repo.user.oauth_access_token
        if not token and repo.user.users:
            token = repo.user.users[0].oauth_access_token

        data = {
            "access_token": token,
            "hub.mode": ['unsubscribe', 'subscribe'][getattr(repo, attr)],
            # TODO -- use our real url
            "hub.callback": "http://pep8.me/webhook",
        }

        for event in github_events:
            data["hub.topic"] = "https://github.com/%s/%s/events/%s" % (
                repo.user.username, repo.name, event)
            # Subscribe to events via pubsubhubbub
            result = requests.post(github_api_url, data=data)

            if result.status_code != 204:
                d = result.json
                if callable(d):
                    d = d()

                d = dict(d)
                d['status_code'] = result.status_code
                raise IOError(d)

    response = {
        'status': 'ok',
        'repo': request.context.__json__(),
        'user': repo.user.username,
        'kind': kind,
    }
    response.update(dict(zip(
        possible_attrs,
        [getattr(request.context, a) for a in possible_attrs]
    )))
    return response


@view_config(context="tw2.core.widgets.WidgetMeta",
             renderer='widget.mak')
def widget_view(request):
    return dict(widget=request.context)
