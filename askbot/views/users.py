"""
:synopsis: user-centric views for askbot

This module includes all views that are specific to a given user - his or her profile,
and other views showing profile-related information.

Also this module includes the view listing all forum users.
"""
import calendar
import collections
import functools
import datetime
import logging
import operator

from django.db.models import Count, Q
from django.conf import settings as django_settings
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator, EmptyPage, InvalidPage
from django.contrib.contenttypes.models import ContentType
from django.core.urlresolvers import reverse
from django.shortcuts import get_object_or_404
from django.http import Http404, HttpResponseRedirect
from django.utils.translation import ugettext_lazy as _
from django.utils import simplejson
from django.views.decorators import csrf

from askbot.utils.slug import slugify
from askbot.utils.html import sanitize_html
from askbot.mail import send_mail
from askbot.utils.http import get_request_info
from askbot.utils import functions
from askbot import forms
from askbot import const
from askbot.conf import settings as askbot_settings
from askbot import models
from askbot import exceptions
from askbot.models.badges import award_badges_signal
from askbot.skins.loaders import render_into_skin
from askbot.search.state_manager import SearchState
from askbot.utils import url_utils
from askbot.utils.loading import load_module
from userena.utils import get_profile_model

def owner_or_moderator_required(f):
    @functools.wraps(f)
    def wrapped_func(request, profile_owner, context):
        if profile_owner == request.user:
            pass
        elif request.user.is_authenticated() and request.user.can_moderate_user(profile_owner):
            pass
        else:
            params = '?next=%s' % request.path
            return HttpResponseRedirect(url_utils.get_login_url() + params)
        return f(request, profile_owner, context)
    return wrapped_func

def show_users(request, by_group = False, group_id = None, group_slug = None):
    """Users view, including listing of users by group"""
#    users = models.User.objects.exclude(status = 'b')
    group = None
    group_email_moderation_enabled = False
    user_can_join_group = False
    user_is_group_member = False
    user_profiles = None
    if by_group == True:
        if askbot_settings.GROUPS_ENABLED == False:
            raise Http404
        if group_id:
            if all((group_id, group_slug)) == False:
                return HttpResponseRedirect('groups')
            else:
                try:
                    group = models.Tag.group_tags.get(id = group_id)
                    group_email_moderation_enabled = \
                        (
                            askbot_settings.GROUP_EMAIL_ADDRESSES_ENABLED \
                            and askbot_settings.ENABLE_CONTENT_MODERATION
                        )
                    user_can_join_group = group.group_profile.can_accept_user(request.user)
                except models.Tag.DoesNotExist:
                    raise Http404
                if group_slug == slugify(group.name):
                    user_profiles = get_profile_model().objects.filter(user__group_memberships__group__id = group_id)
                    if request.user.is_authenticated():
                        user_is_group_member = bool(user_profiles.filter(user__id=request.user.id).count())
                else:
                    group_page_url = reverse(
                                        'users_by_group',
                                        kwargs = {
                                            'group_id': group.id,
                                            'group_slug': slugify(group.name)
                                        }
                                    )
                    return HttpResponseRedirect(group_page_url)
            

    is_paginated = True

    sortby = request.GET.get('sort', 'reputation')
    if askbot_settings.KARMA_MODE == 'private' and sortby == 'reputation':
        sortby = 'newest'

    try:
        page = int(request.GET.get('page', '1'))
    except ValueError:
        page = 1

    search_query = request.REQUEST.get('query',  "")
    if search_query == "":
        if sortby == "newest":
            order_by_parameter = '-user__date_joined'
        elif sortby == "last":
            order_by_parameter = 'user__date_joined'
        elif sortby == "user":
            order_by_parameter = 'user__username'
        else:
            # default
            order_by_parameter = '-reputation'
        
        if not user_profiles:
            user_profiles = get_profile_model().objects.order_by(order_by_parameter)
            
        base_url = request.path + '?sort=%s&amp;' % sortby
    else:
        sortby = "reputation"
        
        if not user_profiles:
            user_profiles = get_profile_model().objects.filter(Q(user__username__icontains=search_query) | Q(about__icontains=search_query)).order_by('-reputation')
        
        base_url = request.path + '?name=%s&amp;sort=%s&amp;' % (search_query, sortby)
    
    objects_list = Paginator(user_profiles,const.USERS_PAGE_SIZE)

    try:
        profiles_page = objects_list.page(page)
    except (EmptyPage, InvalidPage):
        profiles_page = objects_list.page(objects_list.num_pages)

    paginator_data = {
        'is_paginated' : is_paginated,
        'pages': objects_list.num_pages,
        'page': page,
        'has_previous': profiles_page.has_previous(),
        'has_next': profiles_page.has_next(),
        'previous': profiles_page.previous_page_number(),
        'next': profiles_page.next_page_number(),
        'base_url' : base_url
    }
    paginator_context = functions.setup_paginator(paginator_data) #
    data = {
        'active_tab': 'users',
        'page_class': 'users-page',
        'users' : profiles_page,
        'group': group,
        'search_query' : search_query,
        'tab_id' : sortby,
        'paginator_context' : paginator_context,
        'group_email_moderation_enabled': group_email_moderation_enabled,
        'user_can_join_group': user_can_join_group,
        'user_is_group_member': user_is_group_member
    }
    return render_into_skin('users.html', data, request)

@csrf.csrf_protect
def user_moderate(request, subject, context):
    """user subview for moderation
    """
    moderator = request.user

    if moderator.is_anonymous() or (moderator.is_authenticated() and not moderator.can_moderate_user(subject)):
        return HttpResponseRedirect(subject.get_profile().get_absolute_url())

    user_rep_changed = False
    user_status_changed = False
    message_sent = False
    email_error_message = None

    user_rep_form = forms.ChangeUserReputationForm()
    send_message_form = forms.SendMessageForm()
    
    return_to_tab = False
    
    if request.method == 'POST' and request.POST["submitted_tab"] == "moderation":
        if 'change_status' in request.POST:
            user_status_form = forms.ChangeUserStatusForm(
                                                    request.POST,
                                                    moderator = moderator,
                                                    subject = subject
                                                )
            if user_status_form.is_valid():
                subject.set_status( user_status_form.cleaned_data['user_status'] )
            else:
                return_to_tab = True
            user_status_changed = True
        elif 'send_message' in request.POST:
            send_message_form = forms.SendMessageForm(request.POST)
            if send_message_form.is_valid():
                subject_line = send_message_form.cleaned_data['subject_line']
                body_text = send_message_form.cleaned_data['body_text']

                try:
                    send_mail(
                            subject_line = subject_line,
                            body_text = body_text,
                            recipient_list = [subject.email],
                            headers={'Reply-to':moderator.email},
                            raise_on_failure = True
                        )
                    message_sent = True
                except exceptions.EmailNotSent, e:
                    email_error_message = unicode(e)
                send_message_form = forms.SendMessageForm()
            else:
                return_to_tab = True
        else:
            rep_change_type = None
            if 'subtract_reputation' in request.POST:
                rep_change_type = 'subtract'
            elif 'add_reputation' in request.POST:
                rep_change_type = 'add'

            user_rep_form = forms.ChangeUserReputationForm(request.POST)
            if user_rep_form.is_valid():
                rep_delta = user_rep_form.cleaned_data['user_reputation_delta']
                comment = user_rep_form.cleaned_data['comment']

                if rep_change_type == 'subtract':
                    rep_delta = -1 * rep_delta

                moderator.moderate_user_reputation(
                                    user = subject,
                                    reputation_change = rep_delta,
                                    comment = comment,
                                    timestamp = datetime.datetime.now(),
                                )
                #reset form to preclude accidentally repeating submission
                user_rep_form = forms.ChangeUserReputationForm()
                user_rep_changed = True
            else:
                return_to_tab = True
#        return HttpResponseRedirect(subject.get_profile_url())

    #need to re-initialize the form even if it was posted, because
    #initial values will most likely be different from the previous
    user_status_form = forms.ChangeUserStatusForm(
                                        moderator = moderator,
                                        subject = subject
                                    )
    data = {
        'return_to_tab': "moderation" if return_to_tab else "",
        'change_user_status_form': user_status_form,
        'change_user_reputation_form': user_rep_form,
        'send_message_form': send_message_form,
        'message_sent': message_sent,
        'email_error_message': email_error_message,
        'user_rep_changed': user_rep_changed,
        'user_status_changed': user_status_changed
    }
    
    
    return data

#non-view function
def set_new_email(user, new_email, nomessage=False):
    if new_email != user.email:
        user.email = new_email
        user.email_isvalid = False
        user.save()
        #if askbot_settings.EMAIL_VALIDATION == True:
        #    send_new_email_key(user,nomessage=nomessage)

@login_required
@csrf.csrf_protect
def edit_user(request, id):
    """View that allows to edit user profile.
    This view is accessible to profile owners or site administrators
    """
    user = get_object_or_404(models.User, id=id)
    if not(request.user == user or request.user.is_superuser):
        raise Http404
    if request.method == "POST":
        form = forms.EditUserForm(user, request.POST)
        if form.is_valid():
            new_email = sanitize_html(form.cleaned_data['email'])

            set_new_email(user, new_email)

            if askbot_settings.EDITABLE_SCREEN_NAME:
                user.username = sanitize_html(form.cleaned_data['username'])

            user.real_name = sanitize_html(form.cleaned_data['realname'])
            user.website = sanitize_html(form.cleaned_data['website'])
            user.location = sanitize_html(form.cleaned_data['city'])
            user.date_of_birth = form.cleaned_data.get('birthday', None)
            user.about = sanitize_html(form.cleaned_data['about'])
            user.country = form.cleaned_data['country']
            user.show_country = form.cleaned_data['show_country']
            user.show_marked_tags = form.cleaned_data['show_marked_tags']
            user.save()
            # send user updated signal if full fields have been updated
            award_badges_signal.send(None,
                            event = 'update_user_profile',
                            actor = user,
                            context_object = user
                        )
            return HttpResponseRedirect(user.get_profile_url())
    else:
        form = forms.EditUserForm(user)

    data = {
        'active_tab': 'users',
        'page_class': 'user-profile-edit-page',
        'form' : form,
        'marked_tags_setting': askbot_settings.MARKED_TAGS_ARE_PUBLIC_WHEN,
        'support_custom_avatars': False,
        'view_user': user,
    }
    return render_into_skin('user_profile/user_edit.html', data, request)

def user_stats(request, user, context):
    question_filter = {}
    if request.user != user:
        question_filter['is_anonymous'] = False

    if askbot_settings.ENABLE_CONTENT_MODERATION:
        question_filter['approved'] = True

    #
    # Questions
    #
    questions = user.posts.get_questions().filter(**question_filter).\
                    order_by('-score', '-thread__last_activity_at').\
                    select_related('thread', 'thread__last_activity_by')[:100]

    #added this if to avoid another query if questions is less than 100
    if len(questions) < 100:
        question_count = len(questions)
    else:
        question_count = user.posts.get_questions().filter(**question_filter).count()

    #
    # Top answers
    #
    top_answers = user.posts.get_answers().filter(
        deleted=False,
        thread__posts__deleted=False,
        thread__posts__post_type='question',
    ).select_related('thread').order_by('-score', '-added_at')[:100]

    top_answer_count = len(top_answers)

    #
    # Votes
    #
    up_votes = models.Vote.objects.get_up_vote_count_from_user(user)
    down_votes = models.Vote.objects.get_down_vote_count_from_user(user)
    votes_today = models.Vote.objects.get_votes_count_today_from_user(user)
    votes_total = askbot_settings.MAX_VOTES_PER_USER_PER_DAY

    #
    # Tags
    #
    # INFO: There's bug in Django that makes the following query kind of broken (GROUP BY clause is problematic):
    #       http://stackoverflow.com/questions/7973461/django-aggregation-does-excessive-group-by-clauses
    #       Fortunately it looks like it returns correct results for the test data
    user_tags = models.Tag.objects.filter(threads__posts__author=user).distinct().\
                    annotate(user_tag_usage_count=Count('threads')).\
                    order_by('-user_tag_usage_count')[:const.USER_VIEW_DATA_SIZE]
    user_tags = list(user_tags) # evaluate

    when = askbot_settings.MARKED_TAGS_ARE_PUBLIC_WHEN
    if when == 'always' or \
        (when == 'when-user-wants' and user.show_marked_tags == True):
        #refactor into: user.get_marked_tag_names('good'/'bad'/'subscribed')
        interesting_tag_names = user.get_profile().get_marked_tag_names('good')
        ignored_tag_names = user.get_profile().get_marked_tag_names('bad')
        subscribed_tag_names = user.get_profile().get_marked_tag_names('subscribed')
    else:
        interesting_tag_names = None
        ignored_tag_names = None
        subscribed_tag_names = None
        
#    tags = models.Post.objects.filter(author=user).values('id', 'thread', 'thread__tags')
#    post_ids = set()
#    thread_ids = set()
#    tag_ids = set()
#    for t in tags:
#        post_ids.add(t['id'])
#        thread_ids.add(t['thread'])
#        tag_ids.add(t['thread__tags'])
#        if t['thread__tags'] == 11:
#            print t['thread'], t['id']
#    import ipdb; ipdb.set_trace()

    #
    # Badges/Awards (TODO: refactor into Managers/QuerySets when a pattern emerges; Simplify when we get rid of Question&Answer models)
    #
    post_type = ContentType.objects.get_for_model(models.Post)

    user_awards = models.Award.objects.filter(user=user).select_related('badge')

    awarded_post_ids = []
    for award in user_awards:
        if award.content_type_id == post_type.id:
            awarded_post_ids.append(award.object_id)

    awarded_posts = models.Post.objects.filter(id__in=awarded_post_ids)\
                    .select_related('thread') # select related to avoid additional queries in Post.get_absolute_url()

    awarded_posts_map = {}
    for post in awarded_posts:
        awarded_posts_map[post.id] = post

    badges_dict = collections.defaultdict(list)

    for award in user_awards:
        # Fetch content object
        if award.content_type_id == post_type.id:
            #here we go around a possibility of awards
            #losing the content objects when the content
            #objects are deleted for some reason
            awarded_post = awarded_posts_map.get(award.object_id, None)
            if awarded_post is not None:
                #protect from awards that are associated with deleted posts
                award.content_object = awarded_post
                award.content_object_is_post = True
            else:
                award.content_object_is_post = False
        else:
            award.content_object_is_post = False

        # "Assign" to its Badge
        badges_dict[award.badge].append(award)

    badges = badges_dict.items()
    badges.sort(key=operator.itemgetter(1), reverse=True)

    user_groups = models.Tag.group_tags.get_for_user(user = user)

    if request.user == user:
        groups_membership_info = user.get_groups_membership_info(user_groups)
    else:
        groups_membership_info = collections.defaultdict()

    data = {
        'user_status_for_display': user.get_profile().get_status_display(),
        'user_questions' : questions,
        'question_count': question_count,

        'top_answers': top_answers,
        'top_answer_count': top_answer_count,

        'up_votes' : up_votes,
        'down_votes' : down_votes,
        'total_votes': up_votes + down_votes,
        'votes_today_left': votes_total - votes_today,
        'votes_total_per_day': votes_total,

        'user_tags' : user_tags,
        'user_groups': user_groups,
        'groups_membership_info': groups_membership_info,
        'interesting_tag_names': interesting_tag_names,
        'ignored_tag_names': ignored_tag_names,
        'subscribed_tag_names': subscribed_tag_names,
        'badges': badges,
        'total_badges' : len(badges),
    }
    
    return data

def user_recent(request, user, context):

    def get_type_name(type_id):
        for item in const.TYPE_ACTIVITY:
            if type_id in item:
                return item[1]

    class Event(object):
        is_badge = False
        def __init__(self, time, type, title, summary, answer_id, question_id):
            self.time = time
            self.type = get_type_name(type)
            self.type_id = type
            self.title = title
            self.summary = summary
            slug_title = slugify(title)
            self.title_link = reverse(
                                'question',
                                kwargs={'id':question_id}
                            ) + u'%s' % slug_title
            if int(answer_id) > 0:
                self.title_link += '#%s' % answer_id

    class AwardEvent(object):
        is_badge = True
        def __init__(self, time, type, content_object, badge):
            self.time = time
            self.type = get_type_name(type)
            self.content_object = content_object
            self.badge = badge

    activities = []

    # TODO: Don't process all activities here for the user, only a subset ([:const.USER_VIEW_DATA_SIZE])
    for activity in models.Activity.objects.filter(user=user):

        # TODO: multi-if means that we have here a construct for which a design pattern should be used

        # ask questions
        if activity.activity_type == const.TYPE_ACTIVITY_ASK_QUESTION:
            q = activity.content_object
            if q.deleted:
                activities.append(Event(
                    time=activity.active_at,
                    type=activity.activity_type,
                    title=q.thread.title,
                    summary='', #q.summary,  # TODO: was set to '' before, but that was probably wrong
                    answer_id=0,
                    question_id=q.id
                ))

        elif activity.activity_type == const.TYPE_ACTIVITY_ANSWER:
            ans = activity.content_object
            question = ans.thread._question_post()
            if not ans.deleted and not question.deleted:
                activities.append(Event(
                    time=activity.active_at,
                    type=activity.activity_type,
                    title=ans.thread.title,
                    summary=question.summary,
                    answer_id=ans.id,
                    question_id=question.id
                ))

        elif activity.activity_type == const.TYPE_ACTIVITY_COMMENT_QUESTION:
            cm = activity.content_object
            q = cm.parent
            assert q.is_question()
            if not q.deleted:
                activities.append(Event(
                    time=cm.added_at,
                    type=activity.activity_type,
                    title=q.thread.title,
                    summary='',
                    answer_id=0,
                    question_id=q.id
                ))

        elif activity.activity_type == const.TYPE_ACTIVITY_COMMENT_ANSWER:
            cm = activity.content_object
            ans = cm.parent
            assert ans.is_answer()
            question = ans.thread._question_post()
            if not ans.deleted and not question.deleted:
                activities.append(Event(
                    time=cm.added_at,
                    type=activity.activity_type,
                    title=ans.thread.title,
                    summary='',
                    answer_id=ans.id,
                    question_id=question.id
                ))

        elif activity.activity_type == const.TYPE_ACTIVITY_UPDATE_QUESTION:
            q = activity.content_object
            if not q.deleted:
                activities.append(Event(
                    time=activity.active_at,
                    type=activity.activity_type,
                    title=q.thread.title,
                    summary=q.summary,
                    answer_id=0,
                    question_id=q.id
                ))

        elif activity.activity_type == const.TYPE_ACTIVITY_UPDATE_ANSWER:
            ans = activity.content_object
            question = ans.thread._question_post()
            if not ans.deleted and not question.deleted:
                activities.append(Event(
                    time=activity.active_at,
                    type=activity.activity_type,
                    title=ans.thread.title,
                    summary=ans.summary,
                    answer_id=ans.id,
                    question_id=question.id
                ))

        elif activity.activity_type == const.TYPE_ACTIVITY_MARK_ANSWER:
            ans = activity.content_object
            question = ans.thread._question_post()
            if not ans.deleted and not question.deleted:
                activities.append(Event(
                    time=activity.active_at,
                    type=activity.activity_type,
                    title=ans.thread.title,
                    summary='',
                    answer_id=0,
                    question_id=question.id
                ))

        elif activity.activity_type == const.TYPE_ACTIVITY_PRIZE:
            award = activity.content_object
            if award is not None:#todo: work around halfa$$ comment deletion
                activities.append(AwardEvent(
                    time=award.awarded_at,
                    type=activity.activity_type,
                    content_object=award.content_object,
                    badge=award.badge,
                ))

    activities.sort(key=operator.attrgetter('time'), reverse=True)

    data = {
        'activities' : activities[:const.USER_VIEW_DATA_SIZE]
    }
    
    return data

@owner_or_moderator_required
def user_responses(request, user, context):
    """
    We list answers for question, comments, and
    answer accepted by others for this user.
    as well as mentions of the user

    user - the profile owner

    the view has two sub-views - "forum" - i.e. responses
    and "flags" - moderation items for mods only
    """

    #1) select activity types according to section
    section = request.GET.get('section', 'forum')
    if section == 'flags' and not (request.user.is_moderator() or request.user.is_administrator()):
        return {}

    if section == 'forum':
        activity_types = const.RESPONSE_ACTIVITY_TYPES_FOR_DISPLAY
        activity_types += (const.TYPE_ACTIVITY_MENTION,)
    elif section == 'flags':
        activity_types = (const.TYPE_ACTIVITY_MARK_OFFENSIVE,)
        if askbot_settings.ENABLE_CONTENT_MODERATION:
            activity_types += (
                const.TYPE_ACTIVITY_MODERATED_NEW_POST,
                const.TYPE_ACTIVITY_MODERATED_POST_EDIT
            )
    else:
        return {}

    #2) load the activity notifications according to activity types
    #todo: insert pagination code here
    memo_set = models.ActivityAuditStatus.objects.filter(user=request.user, activity__activity_type__in=activity_types)\
                                                 .select_related('activity',
                                                                 'activity__content_type',
                                                                 'activity__question__thread',
                                                                 'activity__user')\
                                                .order_by('-activity__active_at')[:const.USER_VIEW_DATA_SIZE]

    #3) "package" data for the output
    response_list = list()
    for memo in memo_set:
        if memo.activity.content_object is None:
            continue#a temp plug due to bug in the comment deletion
        response = {
            'id': memo.id,
            'timestamp': memo.activity.active_at,
            'user': memo.activity.user,
            'is_new': memo.is_new(),
            'response_url': memo.activity.get_absolute_url(),
            'response_snippet': memo.activity.get_snippet(),
            'response_title': memo.activity.question.thread.title,
            'response_type': memo.activity.get_activity_type_display(),
            'response_id': memo.activity.question.id,
            'nested_responses': [],
            'response_content': memo.activity.content_object.html,
        }
        response_list.append(response)

    #4) sort by response id
    response_list.sort(lambda x,y: cmp(y['response_id'], x['response_id']))

    #5) group responses by thread (response_id is really the question post id)
    last_response_id = None #flag to know if the response id is different
    filtered_response_list = list()
    for i, response in enumerate(response_list):
        #todo: group responses by the user as well
        if response['response_id'] == last_response_id:
            original_response = dict.copy(filtered_response_list[len(filtered_response_list)-1])
            original_response['nested_responses'].append(response)
            filtered_response_list[len(filtered_response_list)-1] = original_response
        else:
            filtered_response_list.append(response)
            last_response_id = response['response_id']

    #6) sort responses by time
    filtered_response_list.sort(lambda x,y: cmp(y['timestamp'], x['timestamp']))

    reject_reasons = models.PostFlagReason.objects.all().order_by('title')
    data = {
        'post_reject_reasons': reject_reasons,
        'responses' : filtered_response_list,
    }
    
    return data

def user_network(request, user, context):
    return {
        'followed_users': user.get_followed_users(),
        'followers': user.get_followers(),
    }

@owner_or_moderator_required
def user_votes(request, user, context):
    data = {
        'votes' : models.Vote.objects.filter(user=user).order_by('-voted_at')[:const.USER_VIEW_DATA_SIZE]
    }
    return data


def user_reputation(request, user, context):
    reputes = models.Repute.objects.filter(user=user).select_related('question', 'question__thread', 'user').order_by('-reputed_at')

    # prepare data for the graph - last values go in first
    rep_list = ['[%s,%s]' % (calendar.timegm(datetime.datetime.now().timetuple()) * 1000, user.get_profile().reputation)]
    for rep in reputes:
        rep_list.append('[%s,%s]' % (calendar.timegm(rep.reputed_at.timetuple()) * 1000, rep.reputation))
    reps = ','.join(rep_list)
    reps = '[%s]' % reps

    data = {
        'reputation': reputes,
        'reps': reps
    }
    return data


def user_favorites(request, user, context):
    favorite_threads = user.user_favorite_questions.values_list('thread', flat=True)
    questions = models.Post.objects.filter(post_type='question', thread__in=favorite_threads)\
                    .select_related('thread', 'thread__last_activity_by')\
                    .order_by('-score', '-thread__last_activity_at')[:const.USER_VIEW_DATA_SIZE]

    data = {
        'favorite_questions' : questions,
    }
    return data


@owner_or_moderator_required
@csrf.csrf_protect
def user_email_subscriptions(request, user, context):
    logging.debug(get_request_info(request))
    action_status = None
    if request.method == 'POST' and request.POST["submitted_tab"] == "email_subscriptions":
        email_feeds_form = forms.EditUserEmailFeedsForm(request.POST)
        tag_filter_form = forms.TagFilterSelectionForm(request.POST, instance=user)
        if email_feeds_form.is_valid() and tag_filter_form.is_valid():

            tag_filter_saved = tag_filter_form.save()
            if tag_filter_saved:
                action_status = _('changes saved')
            if 'save' in request.POST:
                feeds_saved = email_feeds_form.save(user)
                if feeds_saved:
                    action_status = _('changes saved')
            elif 'stop_email' in request.POST:
                email_stopped = email_feeds_form.reset().save(user)
                initial_values = forms.EditUserEmailFeedsForm.NO_EMAIL_INITIAL
                email_feeds_form = forms.EditUserEmailFeedsForm(initial=initial_values)
                if email_stopped:
                    action_status = _('email updates canceled')
#        return HttpResponseRedirect(user.get_profile_url())      
    else:
        #user may have been created by some app that does not know
        #about the email subscriptions, in that case the call below
        #will add any subscription settings that are missing
        #using the default frequencies
        user.get_profile().add_missing_askbot_subscriptions()

        #initialize the form
        email_feeds_form = forms.EditUserEmailFeedsForm()
        email_feeds_form.set_initial_values(user)
        tag_filter_form = forms.TagFilterSelectionForm(instance=user)

    data = {
        'email_feeds_form': email_feeds_form,
        'tag_filter_selection_form': tag_filter_form,
        'action_status': action_status,
    }
    return data

@csrf.csrf_protect
def user_custom_tab(request, user, context):
    """works only if `ASKBOT_CUSTOM_USER_PROFILE_TAB`
    setting in the ``settings.py`` is properly configured"""
    tab_settings = django_settings.ASKBOT_CUSTOM_USER_PROFILE_TAB
    module_path = tab_settings['CONTENT_GENERATOR']
    content_generator = load_module(module_path)

    page_title = _('profile - %(section)s') % \
        {'section': tab_settings['NAME']}

    return {
        'custom_tab_content': content_generator(request, user),
        'tab_name': tab_settings['SLUG'],
        'page_title': page_title
    }

USER_VIEW_CALL_TABLE = {
    'stats': user_stats,
    'recent': user_recent,
    'inbox': user_responses,
    'network': user_network,
    'reputation': user_reputation,
    'favorites': user_favorites,
    'votes': user_votes,
    'email_subscriptions': user_email_subscriptions,
    'moderation': user_moderate,
}

#CUSTOM_TAB = getattr(django_settings, 'ASKBOT_CUSTOM_USER_PROFILE_TAB', None)
#if CUSTOM_TAB:
#    CUSTOM_SLUG = CUSTOM_TAB['SLUG']
#    USER_VIEW_CALL_TABLE[CUSTOM_SLUG] = user_custom_tab

def user_profile(request, id, slug=None, tab_name=None, content_only=False):
    """Main user view function that works as a switchboard

    id - id of the profile owner

    todo: decide what to do with slug - it is not used
    in the code in any way
    """
    profile_owner = get_object_or_404(models.User, id = id)
    
    if askbot_settings.KARMA_MODE == 'public':
        can_show_karma = True
    elif askbot_settings.KARMA_MODE == 'hidden':
        can_show_karma = False
    else:
        if request.user.is_anonymous():
            can_show_karma = False
        elif request.user.is_administrator_or_moderator():
            can_show_karma = True
        elif request.user == profile_owner:
            can_show_karma = True
        else:
            can_show_karma = False

    if can_show_karma == False and tab_name == 'reputation':
        raise Http404

    search_state = SearchState( # Non-default SearchState with user data set
        scope=None,
        sort=None,
        query=None,
        tags=None,
        author=profile_owner.id,
        page=None,
        user_logged_in=profile_owner.is_authenticated(),
    )

    context = {
        'view_user': profile_owner,
        'can_show_karma': can_show_karma,
        'search_state': search_state,
        'tab_name' : tab_name,
        'user_follow_feature_on': True, # ('followit' in django_settings.INSTALLED_APPS),
    }
#    if CUSTOM_TAB:
#        context['custom_tab_name'] = CUSTOM_TAB['NAME']
#        context['custom_tab_slug'] = CUSTOM_TAB['SLUG']
    
    for x, callback in USER_VIEW_CALL_TABLE.iteritems():
        data = callback(request, profile_owner, context)
        if isinstance(data, dict):
            context.update(data)

    if content_only:
        return render_into_skin('user_profile/user_profile_content.html', context, request, to_string=True)
    
    return render_into_skin('user_profile/user.html', context, request)

def groups(request, id = None, slug = None):
    """output groups page
    """
    if askbot_settings.GROUPS_ENABLED == False:
        raise Http404
    
    user = request.user
    profile = user.get_profile()
    
    #6 lines of input cleaning code
    if user.is_authenticated():
        scope = request.GET.get('sort', 'all-groups')
        if scope not in ('all-groups', 'my-groups'):
            scope = 'all-groups'
    else:
        scope = 'all-groups'

    if scope == 'all-groups':
        groups = models.Tag.group_tags.get_all()
    else:
        groups = models.Tag.group_tags.get_for_user(user=user)

    groups = groups.select_related('group_profile')

    user_can_add_groups = user.is_authenticated() and user.is_administrator_or_moderator()

    groups_membership_info = collections.defaultdict()
    if user.is_authenticated():
        #collect group memberhship information
        groups_membership_info = profile.get_groups_membership_info(groups)

    data = {
        'groups': groups,
        'groups_membership_info': groups_membership_info,
        'user_can_add_groups': user_can_add_groups,
        'active_tab': 'groups',#todo vars active_tab and tab_name are too similar
        'tab_name': scope,
        'page_class': 'groups-page'
    }
    return render_into_skin('groups.html', data, request)
