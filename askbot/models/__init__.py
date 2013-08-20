#import collections
import datetime
import uuid
import logging
import urllib
from django.core.urlresolvers import reverse, NoReverseMatch
from django.db.models import signals as django_signals
from django.template import Context
from django.utils.translation import ugettext as _
from django.utils.translation import ungettext
from django.contrib.auth.models import User#, SiteProfileNotAvailable
#from django.utils.safestring import mark_safe
#from django.utils.html import escape
from django.db import models
from django.conf import settings as django_settings
from django.contrib.contenttypes.models import ContentType
from django.core import cache
from django.utils import translation
#from django.core import exceptions as django_exceptions
#from django_countries.fields import CountryField
#from askbot import exceptions as askbot_exceptions
from askbot import const
#from askbot.const.message_keys import get_i18n_message
from askbot.conf import settings as askbot_settings
from askbot.models.question import Thread
#from askbot.skins import utils as skin_utils
from askbot.models.question import QuestionView, AnonymousQuestion
from askbot.models.question import FavoriteQuestion
from askbot.models.tag import Tag, MarkedTag
from askbot.models.user import EmailFeedSetting, ActivityAuditStatus, Activity
from askbot.models.user import GroupMembership, GroupProfile
from askbot.models.post import Post, PostRevision, PostFlagReason, AnonymousAnswer
from askbot.models.reply_by_email import ReplyAddress
from askbot.models import signals
from askbot.models.badges import award_badges_signal, get_badge, BadgeData
from askbot.models.repute import Award, Repute, Vote
#from askbot import auth
#from askbot.utils.decorators import auto_now_timestamp
from askbot.utils.slug import slugify
from askbot.utils.html import sanitize_html
from askbot.utils.diff import textDiff as htmldiff
from askbot.utils.url_utils import strip_path
from askbot import mail
from django.contrib import messages
from userena.utils import get_profile_model
#
#from askbot.models.profile import AskbotProfile

if hasattr(django_settings, 'ASKBOT_STARTUP_CHECK') and django_settings.ASKBOT_STARTUP_CHECK == True:
    from askbot import startup_procedures
    startup_procedures.run()

def get_model(model_name):
    """a shortcut for getting model for an askbot app"""
    return models.get_model('askbot', model_name)

def get_admins_and_moderators():
    """returns query set of users who are site administrators
    and moderators"""
    user_ids = get_profile_model().objects.filter(models.Q(user__is_superuser=True) | models.Q(status='m')).values_list('user', flat=True)
    return User.objects.filter(id__in=user_ids)

def get_users_by_text_query(search_query):
    """Runs text search in user names and profile.
    For postgres, search also runs against user group names.
    """
    return User.objects.filter(
        models.Q(username__icontains=search_query) |
        models.Q(about__icontains=search_query)
    )

#todo: move this to askbot/mail ?
def format_instant_notification_email(
                                        to_user = None,
                                        from_user = None,
                                        post = None,
                                        reply_address = None,
                                        alt_reply_address = None,
                                        update_type = None,
                                        template = None,
                                    ):
    """
    returns text of the instant notification body
    and subject line

    that is built when post is updated
    only update_types in const.RESPONSE_ACTIVITY_TYPE_MAP_FOR_TEMPLATES
    are supported
    """
    origin_post = post.get_origin_post()

    translation.activate(origin_post.thread.language_code)

    context = {
       'post' : post,
       'user_subscriptions_url' : reverse('user_subscriptions', kwargs = {'id': to_user.id, 
                                                                          'slug': slugify(to_user.username)})
    }
    
    if update_type == 'question_comment':
        assert(isinstance(post, Post) and post.is_comment())
        assert(post.parent and post.parent.is_question())
    elif update_type == 'answer_comment':
        assert(isinstance(post, Post) and post.is_comment())
        assert(post.parent and post.parent.is_answer())
    elif update_type == 'answer_update':
        assert(isinstance(post, Post) and post.is_answer())
    elif update_type == 'new_answer':
        assert(isinstance(post, Post) and post.is_answer())
    elif update_type == 'question_update':
        assert(isinstance(post, Post) and post.is_question())
    elif update_type == 'new_question':
        assert(isinstance(post, Post) and post.is_question())
    elif update_type == 'post_shared':
        pass
    else:
        raise ValueError('unexpected update_type %s' % update_type)

    if update_type.endswith('update'):
        assert('comment' not in update_type)
        revisions = post.revisions.all()[:2]
        assert(len(revisions) == 2)
        content_preview = htmldiff(
                sanitize_html(revisions[1].html),
                sanitize_html(revisions[0].html),
                ins_start = '<b><u style="background-color:#cfc">',
                ins_end = '</u></b>',
                del_start = '<del style="color:#600;background-color:#fcc">',
                del_end = '</del>'
            )
        #todo: remove hardcoded style
    else:
        content_preview = post.format_for_email(is_leaf_post = True)

    #add indented summaries for the parent posts
    content_preview += post.format_for_email_as_parent_thread_summary()

    if update_type == 'post_shared':
        user_action = _('%(user)s shared a %(post_link)s.')
    elif post.is_comment():
        if update_type.endswith('update'):
            user_action = _('%(user)s edited a %(post_link)s.')
        else:
            user_action = _('%(user)s posted a %(post_link)s')
    elif post.is_answer():
        if update_type.endswith('update'):
            user_action = _('%(user)s edited an %(post_link)s.')
        else:
            user_action = _('%(user)s posted an %(post_link)s.')
    elif post.is_question():
        if update_type.endswith('update'):
            user_action = _('%(user)s edited a %(post_link)s.')
        else:
            user_action = _('%(user)s posted a %(post_link)s.')
    else:
        raise ValueError('unrecognized post type')
    
    user_action = user_action % {'user' : from_user.get_profile().get_full_name_or_username(),
                                 'post_link' : '<a href="http://imaginationforpeople.org/%s">%s</a>' % (post.get_absolute_url(),
                                                                                                        _(post.post_type))}

    can_reply = to_user.get_profile().can_post_by_email()
    
    if can_reply:
        reply_separator = const.SIMPLE_REPLY_SEPARATOR_TEMPLATE % \
                    _('To reply, PLEASE WRITE ABOVE THIS LINE.')
        if post.post_type == 'question' and alt_reply_address:
            data = {
                'addr': alt_reply_address,
                'subject': urllib.quote(
                        ('Re: ' + post.thread.title).encode('utf-8')
                    )
            }
            reply_separator += '<p>' + \
                const.REPLY_WITH_COMMENT_TEMPLATE % data
            reply_separator += '</p>'
        else:
            reply_separator = '<p>%s</p>' % reply_separator

        reply_separator += user_action
    else:
        reply_separator = user_action
    
    context.update({
        'user_action' : user_action,
        'post_type' : _(post.post_type),
        'update_author': from_user.get_profile(),
        'receiving_user': to_user.get_profile(),
        'receiving_user_karma': to_user.get_profile().reputation,
        'reply_by_email_karma_threshold': askbot_settings.MIN_REP_TO_POST_BY_EMAIL,
        'can_reply': can_reply,
        'content_preview': content_preview,
        'update_type': update_type,
        'origin_post': post.get_origin_post(),
        
        'recipient_user': to_user.get_profile(),
        'reply_separator': reply_separator,
        'reply_address': reply_address,
    })
    
    subject_line = _('"%(title)s"') % {'title': origin_post.thread.title}
    content = template.render(Context(context))
    
    translation.deactivate()
    return subject_line, content

def get_reply_to_addresses(user, post):
    """Returns one or two email addresses that can be
    used by a given `user` to reply to the `post`
    the first address - always a real email address,
    the second address is not ``None`` only for "question" posts.

    When the user is notified of a new question - 
    i.e. `post` is a "quesiton", he/she
    will need to choose - whether to give a question or a comment,
    thus we return the second address - for the comment reply.

    When the post is a "question", the first email address
    is for posting an "answer", and when post is either
    "comment" or "answer", the address will be for posting
    a "comment".
    """
    #these variables will contain return values
    primary_addr = django_settings.DEFAULT_FROM_EMAIL
    secondary_addr = None
    if user.get_profile().can_post_by_email():
        if user.get_profile().reputation >= askbot_settings.MIN_REP_TO_POST_BY_EMAIL:

            reply_args = {
                'post': post,
                'user': user,
                'reply_action': 'post_comment'
            }
            if post.post_type in ('answer', 'comment'):
                reply_args['reply_action'] = 'post_comment'
            elif post.post_type == 'question':
                reply_args['reply_action'] = 'post_answer'

            primary_addr = ReplyAddress.objects.create_new(
                                                    **reply_args
                                                ).as_email_address()

            if post.post_type == 'question':
                reply_args['reply_action'] = 'post_comment'
                secondary_addr = ReplyAddress.objects.create_new(
                                                    **reply_args
                                                ).as_email_address()
    return primary_addr, secondary_addr

#todo: action
def send_instant_notifications_about_activity_in_post(
                                                update_activity = None,
                                                post = None,
                                                recipients = None,
                                            ):
    #reload object from the database
    post = Post.objects.get(id=post.id)
    if post.is_approved() is False:
        return

    if recipients is None:
        return

    acceptable_types = const.RESPONSE_ACTIVITY_TYPES_FOR_INSTANT_NOTIFICATIONS

    if update_activity.activity_type not in acceptable_types:
        return

    #calculate some variables used in the loop below
    from askbot.skins.loaders import get_template
    update_type_map = const.RESPONSE_ACTIVITY_TYPE_MAP_FOR_TEMPLATES
    update_type = update_type_map[update_activity.activity_type]
    origin_post = post.get_origin_post()
    headers = mail.thread_headers(
                            post,
                            origin_post,
                            update_activity.activity_type
                        )

    logger = logging.getLogger()
    if logger.getEffectiveLevel() <= logging.DEBUG:
        log_id = uuid.uuid1()
        message = 'email-alert %s, logId=%s' % (post.get_absolute_url(), log_id)
        logger.debug(message)
    else:
        log_id = None


    for user in recipients:
        reply_address, alt_reply_address = get_reply_to_addresses(user, post)

        subject_line, body_text = format_instant_notification_email(
                            to_user = user,
                            from_user = update_activity.user,
                            post = post,
                            reply_address = reply_address,
                            alt_reply_address = alt_reply_address,
                            update_type = update_type,
                            template = get_template('instant_notification.html')
                        )
      
        headers['Reply-To'] = reply_address
        try:
            mail.send_mail(
                subject_line=subject_line,
                body_text=body_text,
                recipient_list=[user.email],
                related_object=origin_post,
                activity_type=const.TYPE_ACTIVITY_EMAIL_UPDATE_SENT,
                headers=headers,
                raise_on_failure=True
            )
        except askbot_exceptions.EmailNotSent, error:
            logger.debug(
                '%s, error=%s, logId=%s' % (user.email, error, log_id)
            )
        else:
            logger.debug('success %s, logId=%s' % (user.email, log_id))

def notify_author_of_published_revision(
    revision = None, was_approved = None, **kwargs
):
    """notifies author about approved post revision,
    assumes that we have the very first revision
    """
    #only email about first revision
    if revision.should_notify_author_about_publishing(was_approved):
        from askbot.tasks import notify_author_of_published_revision_celery_task
        notify_author_of_published_revision_celery_task.delay(revision)

def record_post_update_activity(
        post,
        newly_mentioned_users = None,
        updated_by = None,
        timestamp = None,
        created = False,
        diff = None,
        **kwargs
    ):
    """called upon signal askbot.models.signals.post_updated
    which is sent at the end of save() method in posts

    this handler will set notifications about the post
    """
    if post.needs_moderation():
        #do not give notifications yet
        #todo: it is possible here to trigger
        #moderation email alerts
        return

    assert(timestamp != None)
    assert(updated_by != None)
    if newly_mentioned_users is None:
        newly_mentioned_users = list()

    from askbot import tasks

    tasks.record_post_update_celery_task.delay(
        post_id = post.id,
        post_content_type_id = ContentType.objects.get_for_model(post).id,
        newly_mentioned_user_id_list = [u.id for u in newly_mentioned_users],
        updated_by_id = updated_by.id,
        timestamp = timestamp,
        created = created,
        diff = diff,
    )


def record_award_event(instance, created, **kwargs):
    """
    After we awarded a badge to user, we need to
    record this activity and notify user.
    We also recaculate awarded_count of this badge and user information.
    """
    if created:
        try:
            profile = instance.user.get_profile()
        except:
            return
            
        #todo: change this to community user who gives the award
        activity = Activity(
                        user=profile.user,
                        active_at=instance.awarded_at,
                        content_object=instance,
                        activity_type=const.TYPE_ACTIVITY_PRIZE
                    )
        activity.save()
        activity.add_recipients([instance.user])

        instance.badge.awarded_count += 1
        instance.badge.save()

        badge = get_badge(instance.badge.slug)
        
        if badge.level == const.GOLD_BADGE:
            profile.gold += 1
        if badge.level == const.SILVER_BADGE:
            profile.silver += 1
        if badge.level == const.BRONZE_BADGE:
            profile.bronze += 1
        profile.save()

def notify_award_message(instance, created, **kwargs):
    """
    Notify users when they have been awarded badges by using Django message.
    """
    if askbot_settings.BADGES_MODE != 'public':
        return
    if created:
        try:
            profile = instance.user.get_profile()
        except:
            return

        badge = get_badge(instance.badge.slug)

        msg = _(u"Congratulations, you have received a badge '%(badge_name)s'. "
                u"Check out <a href=\"%(user_profile)s\">your profile</a>.") \
                % {
                    'badge_name':badge.name,
                    'user_profile':profile.get_absolute_url()
                }

#        user.message_set.create(message=msg)

def record_answer_accepted(instance, created, **kwargs):
    """
    when answer is accepted, we record this for question author
    - who accepted it.
    """
    if instance.post_type != 'answer':
        return

    question = instance.thread._question_post()

    if not created and instance.accepted():
        activity = Activity(
                        user=question.author,
                        active_at=datetime.datetime.now(),
                        content_object=question,
                        activity_type=const.TYPE_ACTIVITY_MARK_ANSWER,
                        question=question
                    )
        activity.save()
        recipients = instance.get_author_list(
                                    exclude_list = [question.author]
                                )
        activity.add_recipients(recipients)

def record_user_visit(user, timestamp, **kwargs):
    """
    when user visits any pages, we update the last_seen and
    consecutive_days_visit_count
    """
    profile = user.get_profile()
    
    prev_last_seen = profile.last_seen or datetime.datetime.now()
    profile.last_seen = timestamp
    if (profile.last_seen - prev_last_seen).days == 1:
        profile.consecutive_days_visit_count += 1
        award_badges_signal.send(None,
                                 event = 'site_visit',
                                 actor = user,
                                 context_object = user,
                                 timestamp = timestamp)

    profile.save()


def record_vote(instance, created, **kwargs):
    """
    when user have voted
    """
    if created:
        if instance.vote == 1:
            vote_type = const.TYPE_ACTIVITY_VOTE_UP
        else:
            vote_type = const.TYPE_ACTIVITY_VOTE_DOWN

        activity = Activity(
                        user=instance.user,
                        active_at=instance.voted_at,
                        content_object=instance,
                        activity_type=vote_type
                    )
        #todo: problem cannot access receiving user here
        activity.save()


def record_cancel_vote(instance, **kwargs):
    """
    when user canceled vote, the vote will be deleted.
    """
    activity = Activity(
                    user=instance.user,
                    active_at=datetime.datetime.now(),
                    content_object=instance,
                    activity_type=const.TYPE_ACTIVITY_CANCEL_VOTE
                )
    #todo: same problem - cannot access receiving user here
    activity.save()


#todo: weird that there is no record delete answer or comment
#is this even necessary to keep track of?
def record_delete_question(instance, delete_by, **kwargs):
    """
    when user deleted the question
    """
    if instance.post_type == 'question':
        activity_type = const.TYPE_ACTIVITY_DELETE_QUESTION
    elif instance.post_type == 'answer':
        activity_type = const.TYPE_ACTIVITY_DELETE_ANSWER
    else:
        return

    activity = Activity(
                    user=delete_by,
                    active_at=datetime.datetime.now(),
                    content_object=instance,
                    activity_type=activity_type,
                    question = instance.get_origin_post()
                )
    #no need to set receiving user here
    activity.save()

def record_flag_offensive(instance, mark_by, **kwargs):
    activity = Activity(
                    user=mark_by,
                    active_at=datetime.datetime.now(),
                    content_object=instance,
                    activity_type=const.TYPE_ACTIVITY_MARK_OFFENSIVE,
                    question=instance.get_origin_post()
                )
    activity.save()
#   todo: report authors that their post is flagged offensive
#    recipients = instance.get_author_list(
#                                        exclude_list = [mark_by]
#                                    )
    activity.add_recipients(get_admins_and_moderators())

def remove_flag_offensive(instance, mark_by, **kwargs):
    "Remove flagging activity"
    content_type = ContentType.objects.get_for_model(instance)

    activity = Activity.objects.filter(
                    user=mark_by,
                    content_type = content_type,
                    object_id = instance.id,
                    activity_type=const.TYPE_ACTIVITY_MARK_OFFENSIVE,
                    question=instance.get_origin_post()
                )
    activity.delete()


def record_update_tags(thread, tags, user, timestamp, **kwargs):
    """
    This function sends award badges signal on each updated tag
    the badges that respond to the 'ta
    """
    for tag in tags:
        award_badges_signal.send(None,
            event = 'update_tag',
            actor = user,
            context_object = tag,
            timestamp = timestamp
        )

    question = thread._question_post()

    activity = Activity(
                    user=user,
                    active_at=datetime.datetime.now(),
                    content_object=question,
                    activity_type=const.TYPE_ACTIVITY_UPDATE_TAGS,
                    question = question
                )
    activity.save()

def record_favorite_question(instance, created, **kwargs):
    """
    when user add the question in him favorite questions list.
    """
    if created:
        activity = Activity(
                        user=instance.user,
                        active_at=datetime.datetime.now(),
                        content_object=instance,
                        activity_type=const.TYPE_ACTIVITY_FAVORITE,
                        question=instance.thread._question_post()
                    )
        activity.save()
        recipients = instance.thread._question_post().get_author_list(
                                            exclude_list = [instance.user]
                                        )
        activity.add_recipients(recipients)

def record_user_full_updated(instance, **kwargs):
    activity = Activity(
                    user=instance,
                    active_at=datetime.datetime.now(),
                    content_object=instance,
                    activity_type=const.TYPE_ACTIVITY_USER_FULL_UPDATED
                )
    activity.save()

def send_respondable_email_validation_message(
    user = None, subject_line = None, data = None, template_name = None
):
    """sends email validation message to the user

    We validate email by getting user's reply
    to the validation message by email, which also gives
    an opportunity to extract user's email signature.
    """
    reply_address = ReplyAddress.objects.create_new(
                                    user = user,
                                    reply_action = 'validate_email'
                                )
    data['email_code'] = reply_address.address

    from askbot.skins.loaders import get_template
    template = get_template(template_name)
    body_text = template.render(Context(data))

    reply_to_address = 'welcome-%s@%s' % (
                            reply_address.address,
                            askbot_settings.REPLY_BY_EMAIL_HOSTNAME
                        )

    mail.send_mail(
        subject_line = subject_line,
        body_text = body_text,
        recipient_list = [user.email, ],
        activity_type = const.TYPE_ACTIVITY_VALIDATION_EMAIL_SENT,
        headers = {'Reply-To': reply_to_address}
    )


def greet_new_user(user, **kwargs):
    """sends welcome email to the newly created user

    todo: second branch should send email with a simple
    clickable link.
    """
#    if askbot_settings.NEW_USER_GREETING:
#        user.message_set.create(message = askbot_settings.NEW_USER_GREETING)

    if askbot_settings.REPLY_BY_EMAIL:#with this on we also collect signature
        template_name = 'email/welcome_lamson_on.html'
    else:
        template_name = 'email/welcome_lamson_off.html'

    data = {
        'site_name': askbot_settings.APP_SHORT_NAME
    }
    send_respondable_email_validation_message(
        user = user,
        subject_line = _('Welcome to %(site_name)s') % data,
        data = data,
        template_name = template_name
    )


def complete_pending_tag_subscriptions(sender, request, *args, **kwargs):
    """save pending tag subscriptions saved in the session"""
    if 'subscribe_for_tags' in request.session:
        (pure_tag_names, wildcards) = request.session.pop('subscribe_for_tags')
        if askbot_settings.SUBSCRIBED_TAG_SELECTOR_ENABLED:
            reason = 'subscribed'
        else:
            reason = 'good'
        request.user.mark_tags(
                    pure_tag_names,
                    wildcards,
                    reason = reason,
                    action = 'add'
                )
        messages.info(request, _('Your tag subscription was saved, thanks!'))

def post_anonymous_askbot_content(
                                sender,
                                request,
                                user,
                                session_key,
                                signal,
                                *args,
                                **kwargs):
    """signal handler, unfortunately extra parameters
    are necessary for the signal machinery, even though
    they are not used in this function"""
    user.get_profile().post_anonymous_askbot_content(request, session_key)


django_signals.post_save.connect(record_award_event, sender=Award)
django_signals.post_save.connect(notify_award_message, sender=Award)
django_signals.post_save.connect(record_answer_accepted, sender=Post)
django_signals.post_save.connect(record_vote, sender=Vote)
django_signals.post_save.connect(record_favorite_question, sender=FavoriteQuestion)

django_signals.post_delete.connect(record_cancel_vote, sender=Vote)

#change this to real m2m_changed with Django1.2
signals.delete_question_or_answer.connect(record_delete_question, sender=Post)
signals.flag_offensive.connect(record_flag_offensive, sender=Post)
signals.remove_flag_offensive.connect(remove_flag_offensive, sender=Post)
signals.tags_updated.connect(record_update_tags)
signals.user_registered.connect(greet_new_user)
signals.user_updated.connect(record_user_full_updated, sender=User)
signals.user_logged_in.connect(complete_pending_tag_subscriptions)#todo: add this to fake onlogin middleware
signals.user_logged_in.connect(post_anonymous_askbot_content)
signals.post_updated.connect(record_post_update_activity)

#probably we cannot use post-save here the point of this is
#to tell when the revision becomes publicly visible, not when it is saved
signals.post_revision_published.connect(notify_author_of_published_revision)
signals.site_visited.connect(record_user_visit)

#set up a possibility for the users to follow others
import followit
followit.register(User)

__all__ = [
        'signals',

        'Thread',

        'QuestionView',
        'FavoriteQuestion',
        'AnonymousQuestion',

        'AnonymousAnswer',

        'Post',
        'PostRevision',

        'Tag',
        'Vote',
        'PostFlagReason',
        'MarkedTag',

        'BadgeData',
        'Award',
        'Repute',

        'Activity',
        'ActivityAuditStatus',
        'EmailFeedSetting',
        'GroupMembership',
        'GroupProfile',

        'User',
        'AskbotProfile',

        'ReplyAddress',

        'get_model',
        'get_admins_and_moderators'
]
