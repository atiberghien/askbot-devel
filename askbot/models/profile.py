import collections
import datetime
import logging

from django.db import models
from django.db.models import signals as django_signals
from django.conf import settings as django_settings
from django.core import exceptions as django_exceptions
from django.contrib.auth.models import User
from django.contrib.contenttypes.models import ContentType
from django.utils.html import escape
from django.utils.translation import ugettext as _
from django.utils.translation import ungettext

from django_countries.fields import CountryField

from askbot import auth
from askbot import const
from askbot import exceptions as askbot_exceptions
from askbot.conf import settings as askbot_settings
from askbot.const.message_keys import get_i18n_message
from askbot.models.question import QuestionView, AnonymousQuestion
from askbot.models.question import FavoriteQuestion
from askbot.models.tag import Tag, MarkedTag
from askbot.models.user import EmailFeedSetting, ActivityAuditStatus, Activity
from askbot.models.user import GroupMembership
from askbot.models.post import Post, PostFlagReason, AnonymousAnswer
from askbot.models.question import Thread
from askbot.models import signals
from askbot.models.badges import award_badges_signal
from askbot.models.repute import Repute, Vote
from askbot.utils.decorators import auto_now_timestamp
from askbot.utils.slug import slugify

from django.utils.safestring import mark_safe

from userena.models import UserenaLanguageBaseProfile
from django.core.urlresolvers import reverse

############################################################
####################### TEMP ###############################
############################################################
"""
This monkey-patching avoid to modify to many piece of code due to the field migration.
Call on user a automatically redirect to related profile if the field is not available in User.
All redirection are logged in order to track call redirection.
Step by step, it will be necessary to rewrite/adapt each piece of code that generation a redirection.
"""

def info(msg):
    import inspect
    import logging
    logger = logging.getLogger('refactoring')
    try:
        frm = inspect.stack()[2]
        caller = frm[3]
        line = frm[2]
        mod = inspect.getmodule(frm[0]).__name__
        info = "%s.%s@%s" % (mod, caller, line)
        if mod.startswith('askbot'):
            logger.debug("[%s] %s" % (info, msg))
    except:
        pass

def user_setattr(self, name, value):
    if '_profile_cache' in self.__dict__:
        profile = self._profile_cache
        if name not in self.__dict__ and \
           name in profile.__dict__ and \
           name != 'profile_must_be_saved':

            info("setattr %s" % name)

            object.__setattr__(profile, name, value)
            self.profile_must_be_saved = True
            return

    object.__setattr__(self, name, value)

def user_getattr(self, name):
    info("getattr %s" % name)
    
    profile = object.__getattribute__(self, 'get_profile')()
    return object.__getattribute__(profile, name)

def get_profile_url(func):
    def wrapped(*args, **kwargs):
        _self = args[0]
        if hasattr(django_settings, 'AUTH_PROFILE_MODULE') and\
            django_settings.AUTH_PROFILE_MODULE:
            return _self.get_profile().get_absolute_url()
        return func(*args, **kwargs)
    return wrapped

if django_settings.AUTH_PROFILE_MODULE != "auth.User":
    setattr(User, 'get_absolute_url', get_profile_url(User.get_absolute_url))
    setattr(User, '__setattr__', user_setattr)
    setattr(User, '__getattr__', user_getattr)
    

############################################################
##################### END TEMP #############################
############################################################

MARKED_TAG_PROPERTY_MAP = {
    'good': 'interesting_tags',
    'bad': 'ignored_tags',
    'subscribed': 'subscribed_tags'
}

VOTES_TO_EVENTS = {
    (Vote.VOTE_UP, 'answer'): 'upvote_answer',
    (Vote.VOTE_UP, 'question'): 'upvote_question',
    (Vote.VOTE_DOWN, 'question'): 'downvote',
    (Vote.VOTE_DOWN, 'answer'): 'downvote',
    (Vote.VOTE_UP, 'comment'): 'upvote_comment',
}

@auto_now_timestamp
def _process_vote(profile, post, timestamp=None, cancel=False, vote_type=None):
    """"private" wrapper function that applies post upvotes/downvotes
    and cancelations
    """
    #get or create the vote object
    #return with noop in some situations
    try:
        vote = Vote.objects.get(user = profile.user, voted_post=post)
    except Vote.DoesNotExist:
        vote = None
    if cancel:
        if vote == None:
            return
        elif vote.is_opposite(vote_type):
            return
        else:
            #we would call vote.delete() here
            #but for now all that is handled by the
            #legacy askbot.auth functions
            #vote.delete()
            pass
    else:
        if vote == None:
            vote = Vote(
                    user = profile.user,
                    voted_post=post,
                    vote = vote_type,
                    voted_at = timestamp,
                )
        elif vote.is_opposite(vote_type):
            vote.vote = vote_type
        else:
            return

    #do the actual work
    if vote_type == Vote.VOTE_UP:
        if cancel:
            auth.onUpVotedCanceled(vote, post, profile.user, timestamp)
        else:
            auth.onUpVoted(vote, post, profile.user, timestamp)
    elif vote_type == Vote.VOTE_DOWN:
        if cancel:
            auth.onDownVotedCanceled(vote, post, profile.user, timestamp)
        else:
            auth.onDownVoted(vote, post, profile.user, timestamp)
            
    post.thread.invalidate_cached_data()

    if post.post_type == 'question':
        #denormalize the question post score on the thread
        post.thread.score = post.score
        post.thread.save()
        post.thread.update_summary_html()

    if cancel:
        return None

    event = VOTES_TO_EVENTS.get((vote_type, post.post_type), None)
    if event:
        award_badges_signal.send(None,
                    event = event,
                    actor = profile.user,
                    context_object = post,
                    timestamp = timestamp
                )
    return vote

def _assert_user_can(profile = None,
                        post = None, #related post (may be parent)
                        admin_or_moderator_required = False,
                        owner_can = False,
                        suspended_owner_cannot = False,
                        owner_min_rep_setting = None,
                        blocked_error_message = None,
                        suspended_error_message = None,
                        min_rep_setting = None,
                        low_rep_error_message = None,
                        owner_low_rep_error_message = None,
                        general_error_message = None):
    """generic helper assert for use in several
    User.assert_can_XYZ() calls regarding changing content

    user is required and at least one error message

    if assertion fails, method raises exception.PermissionDenied
    with appropriate text as a payload
    """
    if blocked_error_message and profile.is_blocked():
        error_message = blocked_error_message
    elif post and owner_can and profile.user == post.get_owner():
        if owner_min_rep_setting:
            if post.get_owner().reputation < owner_min_rep_setting:
                if profile.is_moderator() or profile.is_administrator():
                    return
                else:
                    assert(owner_low_rep_error_message is not None)
                    raise askbot_exceptions.InsufficientReputation(
                                                owner_low_rep_error_message
                                            )
        if suspended_owner_cannot and profile.is_suspended():
            if suspended_error_message:
                error_message = suspended_error_message
            else:
                error_message = general_error_message
            assert(error_message is not None)
            raise django_exceptions.PermissionDenied(error_message)
        else:
            return
        return
    elif suspended_error_message and profile.is_suspended():
        error_message = suspended_error_message
    elif profile.is_administrator() or profile.is_moderator():
        return
    elif low_rep_error_message and profile.reputation < min_rep_setting:
        raise askbot_exceptions.InsufficientReputation(low_rep_error_message)
    else:
        if admin_or_moderator_required == False:
            return

    #if admin or moderator is required, then substitute the message
    if admin_or_moderator_required:
        error_message = general_error_message
    assert(error_message is not None)
    raise django_exceptions.PermissionDenied(error_message)


def get_name_of_anonymous_user():
    """Returns name of the anonymous user
    either comes from the live settyngs or the language
    translation

    very possible that this function does not belong here
    """
    if askbot_settings.NAME_OF_ANONYMOUS_USER:
        return askbot_settings.NAME_OF_ANONYMOUS_USER
    else:
        return _('Anonymous')

class AskbotBaseProfile(models.Model):
    """
    Abstract model that encapsulate Askbot specific stuff.

    Subclass must have :
        user = models.OneToOneField(User)
    """
    status = models.CharField(max_length=2, default=const.DEFAULT_USER_STATUS, choices=const.USER_STATUS_CHOICES)
    reputation = models.PositiveIntegerField(default=const.MIN_REPUTATION)
    gold = models.SmallIntegerField(default=0)
    silver = models.SmallIntegerField(default=0)
    bronze = models.SmallIntegerField(default=0)
    questions_per_page = models.SmallIntegerField(choices=const.QUESTIONS_PER_PAGE_USER_CHOICES, default=10)
    last_seen = models.DateTimeField(default=datetime.datetime.now)
    interesting_tags = models.TextField(blank = True)
    ignored_tags = models.TextField(blank = True)
    subscribed_tags = models.TextField(blank = True)
    show_marked_tags = models.BooleanField(default = True)
    email_tag_filter_strategy = models.SmallIntegerField(choices=const.TAG_DISPLAY_FILTER_STRATEGY_CHOICES, default=const.EXCLUDE_IGNORED)
    email_signature = models.TextField(blank = True)
    display_tag_filter_strategy = models.SmallIntegerField(choices=const.TAG_EMAIL_FILTER_STRATEGY_CHOICES, default=const.INCLUDE_ALL)
    new_response_count = models.IntegerField(default=0)
    seen_response_count = models.IntegerField(default=0)
    consecutive_days_visit_count = models.IntegerField(default=0)
    is_fake = models.BooleanField(default=False)
    
    def strip_email_signature(self, text):
        """strips email signature from the end of the text"""
        if self.email_signature.strip() == '':
            return text
    
        text = '\n'.join(text.splitlines())#normalize the line endings
        while text.endswith(self.email_signature):
            text = text[0:-len(self.email_signature)]
        return text

    def get_old_vote_for_post(self, post):
        """returns previous vote for this post
        by the user or None, if does not exist
    
        raises assertion_error is number of old votes is > 1
        which is illegal
        """
        try:
            return Vote.objects.get(user=self.user, voted_post=post)
        except Vote.DoesNotExist:
            return None
        except Vote.MultipleObjectsReturned:
            raise AssertionError
    
    def get_marked_tags(self, reason):
        """reason is a type of mark: good, bad or subscribed"""
        assert(reason in ('good', 'bad', 'subscribed'))
        if reason == 'subscribed':
            if askbot_settings.SUBSCRIBED_TAG_SELECTOR_ENABLED == False:
                return Tag.objects.none()
    
        return Tag.objects.filter(
            user_selections__user = self.user,
            user_selections__reason = reason
        )

    def get_marked_tag_names(self, reason):
        """returns list of marked tag names for a give
        reason: good, bad, or subscribed
        will add wildcard tags as well, if used
        """
        if reason == 'subscribed':
            if askbot_settings.SUBSCRIBED_TAG_SELECTOR_ENABLED == False:
                return list()
    
        tags = self.get_marked_tags(reason)
        tag_names = list(tags.values_list('name', flat = True))
    
        if askbot_settings.USE_WILDCARD_TAGS:
            attr_name = MARKED_TAG_PROPERTY_MAP[reason]
            wildcard_tags = getattr(self, attr_name).split()
            tag_names.extend(wildcard_tags)
            
        return tag_names
    
    def has_affinity_to_question(self, question = None, affinity_type = None):
        """returns True if number of tag overlap of the user tag
        selection with the question is 0 and False otherwise
        affinity_type can be either "like" or "dislike"
        """
        if affinity_type == 'like':
            if askbot_settings.SUBSCRIBED_TAG_SELECTOR_ENABLED:
                tag_selection_type = 'subscribed'
                wildcards = self.subscribed_tags.split()
            else:
                tag_selection_type = 'good'
                wildcards = self.interesting_tags.split()
        elif affinity_type == 'dislike':
            tag_selection_type = 'bad'
            wildcards = self.ignored_tags.split()
        else:
            raise ValueError('unexpected affinity type %s' % str(affinity_type))
    
        question_tags = question.thread.tags.all()
        intersecting_tag_selections = self.tag_selections.filter(
                                                    tag__in = question_tags,
                                                    reason = tag_selection_type
                                                )
        #count number of overlapping tags
        if intersecting_tag_selections.count() > 0:
            return True
        elif askbot_settings.USE_WILDCARD_TAGS == False:
            return False
    
        #match question tags against wildcards
        for tag in question_tags:
            for wildcard in wildcards:
                if tag.name.startswith(wildcard[:-1]):
                    return True
        return False
    
    def has_ignored_wildcard_tags(self):
        """True if wildcard tags are on and
        user has some"""
        return (
            askbot_settings.USE_WILDCARD_TAGS \
            and self.ignored_tags != ''
        )
    
    def assert_can_approve_post_revision(self, post_revision = None):
        _assert_user_can(
            profile = self,
            admin_or_moderator_required = True
        )
    
    def assert_can_unaccept_best_answer(self, answer = None):
        assert getattr(answer, 'post_type', '') == 'answer'
        blocked_error_message = _(
                'Sorry, you cannot accept or unaccept best answers '
                'because your account is blocked'
            )
        suspended_error_message = _(
                'Sorry, you cannot accept or unaccept best answers '
                'because your account is suspended'
            )
        if self.is_blocked():
            error_message = blocked_error_message
        elif self.is_suspended():
            error_message = suspended_error_message
        elif self == answer.thread._question_post().get_owner():
            if self == answer.get_owner():
                if not self.is_administrator():
                    #check rep
                    min_rep_setting = askbot_settings.MIN_REP_TO_ACCEPT_OWN_ANSWER
                    low_rep_error_message = _(
                                ">%(points)s points required to accept or unaccept "
                                " your own answer to your own question"
                            ) % {'points': min_rep_setting}
    
                    _assert_user_can(
                        profile = self,
                        blocked_error_message = blocked_error_message,
                        suspended_error_message = suspended_error_message,
                        min_rep_setting = min_rep_setting,
                        low_rep_error_message = low_rep_error_message
                    )
            return # success
    
        elif self.is_administrator() or self.is_moderator():
            will_be_able_at = (
                answer.added_at +
                datetime.timedelta(
                    days=askbot_settings.MIN_DAYS_FOR_STAFF_TO_ACCEPT_ANSWER)
            )
    
            if datetime.datetime.now() < will_be_able_at:
                error_message = _(
                    'Sorry, you will be able to accept this answer '
                    'only after %(will_be_able_at)s'
                    ) % {'will_be_able_at': will_be_able_at.strftime('%d/%m/%Y')}
            else:
                return
    
        else:
            error_message = _(
                'Sorry, only moderators or original author of the question '
                ' - %(username)s - can accept or unaccept the best answer'
                ) % {'username': answer.get_owner().username}
    
        raise django_exceptions.PermissionDenied(error_message)
    
    def assert_can_accept_best_answer(self, answer = None):
        assert getattr(answer, 'post_type', '') == 'answer'
        self.assert_can_unaccept_best_answer(answer)
    
    def assert_can_vote_for_post(
                                    self,
                                    post = None,
                                    direction = None,
                                ):
        """raises exceptions.PermissionDenied exception
        if user can't in fact upvote
    
        :param:direction can be 'up' or 'down'
        :param:post can be instance of question or answer
        """
        if self.user == post.author:
            raise django_exceptions.PermissionDenied(
                _('Sorry, you cannot vote for your own posts')
            )
    
        blocked_error_message = _(
                    'Sorry your account appears to be blocked ' +
                    'and you cannot vote - please contact the ' +
                    'site administrator to resolve the issue'
                ),
        suspended_error_message = _(
                    'Sorry your account appears to be suspended ' +
                    'and you cannot vote - please contact the ' +
                    'site administrator to resolve the issue'
                )
    
        assert(direction in ('up', 'down'))
    
        if direction == 'up':
            min_rep_setting = askbot_settings.MIN_REP_TO_VOTE_UP
            low_rep_error_message = _(
                        ">%(points)s points required to upvote"
                    ) % \
                    {'points': askbot_settings.MIN_REP_TO_VOTE_UP}
        else:
            min_rep_setting = askbot_settings.MIN_REP_TO_VOTE_DOWN
            low_rep_error_message = _(
                        ">%(points)s points required to downvote"
                    ) % \
                    {'points': askbot_settings.MIN_REP_TO_VOTE_DOWN}
    
        _assert_user_can(
            profile = self,
            blocked_error_message = blocked_error_message,
            suspended_error_message = suspended_error_message,
            min_rep_setting = min_rep_setting,
            low_rep_error_message = low_rep_error_message
        )
    
    
    def assert_can_upload_file(self):
    
        blocked_error_message = _('Sorry, blocked users cannot upload files')
        suspended_error_message = _('Sorry, suspended users cannot upload files')
        low_rep_error_message = _(
                            'sorry, file uploading requires karma >%(min_rep)s',
                        ) % {'min_rep': askbot_settings.MIN_REP_TO_UPLOAD_FILES }
    
        _assert_user_can(
            profile = self,
            suspended_error_message = suspended_error_message,
            min_rep_setting = askbot_settings.MIN_REP_TO_UPLOAD_FILES,
            low_rep_error_message = low_rep_error_message
        )
    
    
    def assert_can_post_question(self):
        """raises exceptions.PermissionDenied with
        text that has the reason for the denial
        """
    
        blocked_message = get_i18n_message('BLOCKED_USERS_CANNOT_POST')
        suspended_message = get_i18n_message('SUSPENDED_USERS_CANNOT_POST')
    
        _assert_user_can(
                profile = self,
                blocked_error_message = blocked_message,
                suspended_error_message = suspended_message
        )
    
    
    def assert_can_post_answer(self, thread = None):
        """same as user_can_post_question
        """
        limit_answers = askbot_settings.LIMIT_ONE_ANSWER_PER_USER
        if limit_answers and thread.has_answer_by_user(self):
            message = _(
                'Sorry, you already gave an answer, please edit it instead.'
            )
            raise askbot_exceptions.AnswerAlreadyGiven(message)
        
        self.assert_can_post_question()
    
    
    def assert_can_edit_comment(self, comment = None):
        """raises exceptions.PermissionDenied if user
        cannot edit comment with the reason given as message
    
        only owners, moderators or admins can edit comments
        """
        if self.is_administrator() or self.is_moderator():
            return
        else:
            if comment.author == self.user:
                if askbot_settings.USE_TIME_LIMIT_TO_EDIT_COMMENT:
                    now = datetime.datetime.now()
                    delta_seconds = 60 * askbot_settings.MINUTES_TO_EDIT_COMMENT
                    if now - comment.added_at > datetime.timedelta(0, delta_seconds):
                        if comment.is_last():
                            return
                        error_message = ungettext(
                            'Sorry, comments (except the last one) are editable only '
                            'within %(minutes)s minute from posting',
                            'Sorry, comments (except the last one) are editable only '
                            'within %(minutes)s minutes from posting',
                            askbot_settings.MINUTES_TO_EDIT_COMMENT
                        ) % {'minutes': askbot_settings.MINUTES_TO_EDIT_COMMENT}
                        raise django_exceptions.PermissionDenied(error_message)
                    return
                else:
                    return
    
        error_message = _(
            'Sorry, but only post owners or moderators can edit comments'
        )
        raise django_exceptions.PermissionDenied(error_message)
    
    
    def can_post_comment(self, parent_post = None):
        """a simplified method to test ability to comment
        """
        if self.reputation >= askbot_settings.MIN_REP_TO_LEAVE_COMMENTS:
            return True
        if parent_post and self.user == parent_post.author:
            return True
        if self.is_administrator_or_moderator():
            return True
        return False
    
    
    def assert_can_post_comment(self, parent_post = None):
        """raises exceptions.PermissionDenied if
        user cannot post comment
    
        the reason will be in text of exception
        """
    
        suspended_error_message = _(
                    'Sorry, since your account is suspended '
                    'you can comment only your own posts'
                )
        low_rep_error_message = _(
                    'Sorry, to comment any post a minimum reputation of '
                    '%(min_rep)s points is required. You can still comment '
                    'your own posts and answers to your questions'
                ) % {'min_rep': askbot_settings.MIN_REP_TO_LEAVE_COMMENTS}
    
        blocked_message = get_i18n_message('BLOCKED_USERS_CANNOT_POST')
    
        try:
            _assert_user_can(
                profile = self,
                post = parent_post,
                owner_can = True,
                blocked_error_message = blocked_message,
                suspended_error_message = suspended_error_message,
                min_rep_setting = askbot_settings.MIN_REP_TO_LEAVE_COMMENTS,
                low_rep_error_message = low_rep_error_message,
            )
        except askbot_exceptions.InsufficientReputation, e:
            if parent_post.post_type == 'answer':
                if self.user == parent_post.thread._question_post().author:
                    return
            raise e
    
    def assert_can_see_deleted_post(self, post = None):
    
        """attn: this assertion is independently coded in
        Question.get_answers call
        """
    
        error_message = _(
                            'This post has been deleted and can be seen only '
                            'by post owners, site administrators and moderators'
                        )
        _assert_user_can(
            profile = self,
            post = post,
            admin_or_moderator_required = True,
            owner_can = True,
            general_error_message = error_message
        )
    
    def assert_can_edit_deleted_post(self, post = None):
        assert(post.deleted == True)
        try:
            self.assert_can_see_deleted_post(post)
        except django_exceptions.PermissionDenied, e:
            error_message = _(
                        'Sorry, only moderators, site administrators '
                        'and post owners can edit deleted posts'
                    )
            raise django_exceptions.PermissionDenied(error_message)
    
    def assert_can_edit_post(self, post = None):
        """assertion that raises exceptions.PermissionDenied
        when user is not authorised to edit this post
        """
    
        if post.deleted == True:
            self.assert_can_edit_deleted_post(post)
            return
    
        blocked_error_message = _(
                    'Sorry, since your account is blocked '
                    'you cannot edit posts'
                )
        suspended_error_message = _(
                    'Sorry, since your account is suspended '
                    'you can edit only your own posts'
                )
        if post.wiki == True:
            low_rep_error_message = _(
                        'Sorry, to edit wiki posts, a minimum '
                        'reputation of %(min_rep)s is required'
                    ) % \
                    {'min_rep': askbot_settings.MIN_REP_TO_EDIT_WIKI}
            min_rep_setting = askbot_settings.MIN_REP_TO_EDIT_WIKI
        else:
            low_rep_error_message = _(
                        'Sorry, to edit other people\'s posts, a minimum '
                        'reputation of %(min_rep)s is required'
                    ) % \
                    {'min_rep': askbot_settings.MIN_REP_TO_EDIT_OTHERS_POSTS}
            min_rep_setting = askbot_settings.MIN_REP_TO_EDIT_OTHERS_POSTS
    
        _assert_user_can(
            profile = self,
            post = post,
            owner_can = True,
            blocked_error_message = blocked_error_message,
            suspended_error_message = suspended_error_message,
            low_rep_error_message = low_rep_error_message,
            min_rep_setting = min_rep_setting
        )
    
    
    def assert_can_edit_question(self, question = None):
        assert getattr(question, 'post_type', '') == 'question'
        self.assert_can_edit_post(question)
    
    
    def assert_can_edit_answer(self, answer = None):
        assert getattr(answer, 'post_type', '') == 'answer'
        self.assert_can_edit_post(answer)
    
    
    def assert_can_delete_post(self, post = None):
        post_type = getattr(post, 'post_type', '')
        if post_type == 'question':
            self.assert_can_delete_question(question = post)
        elif post_type == 'answer':
            self.assert_can_delete_answer(answer = post)
        elif post_type == 'comment':
            self.assert_can_delete_comment(comment = post)
        else:
            raise ValueError('Invalid post_type!')
    
    def assert_can_restore_post(self, post = None):
        """can_restore_rule is the same as can_delete
        """
        self.assert_can_delete_post(post = post)
    
    def assert_can_delete_question(self, question = None):
        """rules are the same as to delete answer,
        except if question has answers already, when owner
        cannot delete unless s/he is and adinistrator or moderator
        """
    
        #cheating here. can_delete_answer wants argument named
        #"question", so the argument name is skipped
        self.assert_can_delete_answer(question)
        if self == question.get_owner():
            #if there are answers by other people,
            #then deny, unless user in admin or moderator
            answer_count = question.thread.all_answers()\
                            .exclude(author=self.user).exclude(score__lte=0).count()
    
            if answer_count > 0:
                if self.is_administrator() or self.is_moderator():
                    return
                else:
                    msg = ungettext(
                        'Sorry, cannot delete your question since it '
                        'has an upvoted answer posted by someone else',
                        'Sorry, cannot delete your question since it '
                        'has some upvoted answers posted by other users',
                        answer_count
                    )
                    raise django_exceptions.PermissionDenied(msg)
    
    
    def assert_can_delete_answer(self, answer = None):
        """intentionally use "post" word in the messages
        instead of "answer", because this logic also applies to
        assert on deleting question (in addition to some special rules)
        """
        blocked_error_message = _(
                    'Sorry, since your account is blocked '
                    'you cannot delete posts'
                )
        suspended_error_message = _(
                    'Sorry, since your account is suspended '
                    'you can delete only your own posts'
                )
        low_rep_error_message = _(
                    'Sorry, to deleted other people\' posts, a minimum '
                    'reputation of %(min_rep)s is required'
                ) % \
                {'min_rep': askbot_settings.MIN_REP_TO_DELETE_OTHERS_POSTS}
        min_rep_setting = askbot_settings.MIN_REP_TO_DELETE_OTHERS_POSTS
    
        _assert_user_can(
            profile = self,
            post = answer,
            owner_can = True,
            blocked_error_message = blocked_error_message,
            suspended_error_message = suspended_error_message,
            low_rep_error_message = low_rep_error_message,
            min_rep_setting = min_rep_setting
        )
    
    
    def assert_can_close_question(self, question = None):
        assert(getattr(question, 'post_type', '') == 'question')
        blocked_error_message = _(
                    'Sorry, since your account is blocked '
                    'you cannot close questions'
                )
        suspended_error_message = _(
                    'Sorry, since your account is suspended '
                    'you cannot close questions'
                )
        low_rep_error_message = _(
                    'Sorry, to close other people\' posts, a minimum '
                    'reputation of %(min_rep)s is required'
                ) % \
                {'min_rep': askbot_settings.MIN_REP_TO_CLOSE_OTHERS_QUESTIONS}
        min_rep_setting = askbot_settings.MIN_REP_TO_CLOSE_OTHERS_QUESTIONS
    
        owner_min_rep_setting =  askbot_settings.MIN_REP_TO_CLOSE_OWN_QUESTIONS
    
        owner_low_rep_error_message = _(
                            'Sorry, to close own question '
                            'a minimum reputation of %(min_rep)s is required'
                        ) % {'min_rep': owner_min_rep_setting}
    
        _assert_user_can(
            profile = self,
            post = question,
            owner_can = True,
            suspended_owner_cannot = True,
            owner_min_rep_setting = owner_min_rep_setting,
            blocked_error_message = blocked_error_message,
            suspended_error_message = suspended_error_message,
            low_rep_error_message = low_rep_error_message,
            owner_low_rep_error_message = owner_low_rep_error_message,
            min_rep_setting = min_rep_setting
        )
    
    
    def assert_can_reopen_question(self, question = None):
        assert(question.post_type == 'question')
    
        #for some reason rep to reopen own questions != rep to close own q's
        owner_min_rep_setting =  askbot_settings.MIN_REP_TO_REOPEN_OWN_QUESTIONS
        min_rep_setting = askbot_settings.MIN_REP_TO_CLOSE_OTHERS_QUESTIONS
    
        general_error_message = _(
                            'Sorry, only administrators, moderators '
                            'or post owners with reputation > %(min_rep)s '
                            'can reopen questions.'
                        ) % {'min_rep': owner_min_rep_setting }
    
        owner_low_rep_error_message = _(
                            'Sorry, to reopen own question '
                            'a minimum reputation of %(min_rep)s is required'
                        ) % {'min_rep': owner_min_rep_setting}
    
        blocked_error_message = _(
                'Sorry, you cannot reopen questions '
                'because your account is blocked'
            )
    
        suspended_error_message = _(
                'Sorry, you cannot reopen questions '
                'because your account is suspended'
            )
    
        _assert_user_can(
            user = self.user,
            post = question,
            owner_can = True,
            suspended_owner_cannot = True,
            owner_min_rep_setting = owner_min_rep_setting,
            min_rep_setting = min_rep_setting,
            owner_low_rep_error_message = owner_low_rep_error_message,
            general_error_message = general_error_message,
            blocked_error_message = blocked_error_message,
            suspended_error_message = suspended_error_message
        )
    
    
    def assert_can_flag_offensive(self, post = None):
    
        assert(post is not None)
    
        double_flagging_error_message = _(
            'You have flagged this question before and '
            'cannot do it more than once'
        )
    
        if self.get_flags_for_post(post).count() > 0:
            raise askbot_exceptions.DuplicateCommand(double_flagging_error_message)
    
        blocked_error_message = _(
            'Sorry, since your account is blocked '
            'you cannot flag posts as offensive'
        )
    
        suspended_error_message = _(
            'Sorry, your account appears to be suspended and you cannot make new posts '
            'until this issue is resolved. You can, however edit your existing posts. '
            'Please contact the forum administrator to reach a resolution.'
        )
    
        low_rep_error_message = _(
            'Sorry, to flag posts as offensive a minimum reputation '
            'of %(min_rep)s is required'
        ) % \
                            {'min_rep': askbot_settings.MIN_REP_TO_FLAG_OFFENSIVE}
        min_rep_setting = askbot_settings.MIN_REP_TO_FLAG_OFFENSIVE
    
        _assert_user_can(
            profile = self,
            post = post,
            blocked_error_message = blocked_error_message,
            suspended_error_message = suspended_error_message,
            low_rep_error_message = low_rep_error_message,
            min_rep_setting = min_rep_setting
        )
        #one extra assertion
        if self.is_administrator() or self.is_moderator():
            return
        else:
            flag_count_today = self.get_flag_count_posted_today()
            if flag_count_today >= askbot_settings.MAX_FLAGS_PER_USER_PER_DAY:
                flags_exceeded_error_message = _(
                    'Sorry, you have exhausted the maximum number of '
                    '%(max_flags_per_day)s offensive flags per day.'
                ) % {
                        'max_flags_per_day': \
                        askbot_settings.MAX_FLAGS_PER_USER_PER_DAY
                    }
                raise django_exceptions.PermissionDenied(flags_exceeded_error_message)
    
    def assert_can_remove_flag_offensive(self, post = None):
    
        assert(post is not None)
    
        non_existing_flagging_error_message = _('cannot remove non-existing flag')
    
        if self.get_flags_for_post(post).count() < 1:
            raise django_exceptions.PermissionDenied(non_existing_flagging_error_message)
    
        blocked_error_message = _(
            'Sorry, since your account is blocked you cannot remove flags'
        )
    
        suspended_error_message = _(
            'Sorry, your account appears to be suspended and you cannot remove flags. '
            'Please contact the forum administrator to reach a resolution.'
        )
    
        min_rep_setting = askbot_settings.MIN_REP_TO_FLAG_OFFENSIVE
        low_rep_error_message = ungettext(
            'Sorry, to flag posts a minimum reputation of %(min_rep)d is required',
            'Sorry, to flag posts a minimum reputation of %(min_rep)d is required',
            min_rep_setting
        ) % {'min_rep': min_rep_setting}
    
        _assert_user_can(
            profile = self,
            post = post,
            blocked_error_message = blocked_error_message,
            suspended_error_message = suspended_error_message,
            low_rep_error_message = low_rep_error_message,
            min_rep_setting = min_rep_setting
        )
        #one extra assertion
        if self.is_administrator() or self.is_moderator():
            return
    
    def assert_can_remove_all_flags_offensive(self, post = None):
        assert(post is not None)
        permission_denied_message = _("you don't have the permission to remove all flags")
        non_existing_flagging_error_message = _('no flags for this entry')
    
        # Check if the post is flagged by anyone
        post_content_type = ContentType.objects.get_for_model(post)
        all_flags = Activity.objects.filter(
                            activity_type = const.TYPE_ACTIVITY_MARK_OFFENSIVE,
                            content_type = post_content_type, object_id=post.id
                        )
        if all_flags.count() < 1:
            raise django_exceptions.PermissionDenied(non_existing_flagging_error_message)
        #one extra assertion
        if self.is_administrator() or self.is_moderator():
            return
        else:
            raise django_exceptions.PermissionDenied(permission_denied_message)
    
    
    def assert_can_retag_question(self, question = None):
    
        if question.deleted == True:
            try:
                self.assert_can_edit_deleted_post(question)
            except django_exceptions.PermissionDenied:
                error_message = _(
                                'Sorry, only question owners, '
                                'site administrators and moderators '
                                'can retag deleted questions'
                            )
                raise django_exceptions.PermissionDenied(error_message)
    
        blocked_error_message = _(
                    'Sorry, since your account is blocked '
                    'you cannot retag questions'
                )
        suspended_error_message = _(
                    'Sorry, since your account is suspended '
                    'you can retag only your own questions'
                )
        low_rep_error_message = _(
                    'Sorry, to retag questions a minimum '
                    'reputation of %(min_rep)s is required'
                ) % \
                {'min_rep': askbot_settings.MIN_REP_TO_RETAG_OTHERS_QUESTIONS}
        min_rep_setting = askbot_settings.MIN_REP_TO_RETAG_OTHERS_QUESTIONS
    
        _assert_user_can(
            profile = self.user,
            post = question,
            owner_can = True,
            blocked_error_message = blocked_error_message,
            suspended_error_message = suspended_error_message,
            low_rep_error_message = low_rep_error_message,
            min_rep_setting = min_rep_setting
        )
    
    
    def assert_can_delete_comment(self, comment = None):
        blocked_error_message = _(
                    'Sorry, since your account is blocked '
                    'you cannot delete comment'
                )
        suspended_error_message = _(
                    'Sorry, since your account is suspended '
                    'you can delete only your own comments'
                )
        low_rep_error_message = _(
                    'Sorry, to delete comments '
                    'reputation of %(min_rep)s is required'
                ) % \
                {'min_rep': askbot_settings.MIN_REP_TO_DELETE_OTHERS_COMMENTS}
        min_rep_setting = askbot_settings.MIN_REP_TO_DELETE_OTHERS_COMMENTS
    
        _assert_user_can(
            profile = self.user,
            post = comment,
            owner_can = True,
            blocked_error_message = blocked_error_message,
            suspended_error_message = suspended_error_message,
            low_rep_error_message = low_rep_error_message,
            min_rep_setting = min_rep_setting
        )
    
    
    def assert_can_revoke_old_vote(self, vote):
        """raises exceptions.PermissionDenied if old vote
        cannot be revoked due to age of the vote
        """
        if (datetime.datetime.now().day - vote.voted_at.day) \
            >= askbot_settings.MAX_DAYS_TO_CANCEL_VOTE:
            raise django_exceptions.PermissionDenied(
                _('sorry, but older votes cannot be revoked')
            )
    
    def get_unused_votes_today(self):
        """returns number of votes that are
        still available to the user today
        """
        today = datetime.date.today()
        one_day_interval = (today, today + datetime.timedelta(1))
    
        used_votes = Vote.objects.filter(
                                    user = self.user,
                                    voted_at__range = one_day_interval
                                ).count()
    
        available_votes = askbot_settings.MAX_VOTES_PER_USER_PER_DAY - used_votes
        return max(0, available_votes)
    
    def post_comment(self,
                        parent_post = None,
                        body_text = None,
                        timestamp = None,
                        by_email = False
                    ):
        """post a comment on behalf of the user
        to parent_post
        """
    
        if body_text is None:
            raise ValueError('body_text is required to post comment')
        if parent_post is None:
            raise ValueError('parent_post is required to post comment')
        if timestamp is None:
            timestamp = datetime.datetime.now()
    
        self.assert_can_post_comment(parent_post = parent_post)
    
        comment = parent_post.add_comment(
                        user = self.user,
                        comment = body_text,
                        added_at = timestamp,
                        by_email = by_email
                    )
        parent_post.thread.invalidate_cached_data()
        award_badges_signal.send(
            None,
            event = 'post_comment',
            actor = self.user,
            context_object = comment,
            timestamp = timestamp
        )
        return comment
    
    def post_tag_wiki(
                        self,
                        tag = None,
                        body_text = None,
                        timestamp = None
                    ):
        """Creates a tag wiki post and assigns it
        to the given tag. Returns the newly created post"""
        tag_wiki_post = Post.objects.create_new_tag_wiki(
                                                author = self.user,
                                                text = body_text
                                            )
        tag.tag_wiki = tag_wiki_post
        tag.save()
        return tag_wiki_post
    
    
    def post_anonymous_askbot_content(self, session_key):
        """posts any posts added just before logging in
        the posts are identified by the session key, thus the second argument
    
        this function is used by the signal handler with a similar name
        """
        aq_list = AnonymousQuestion.objects.filter(session_key = session_key)
        aa_list = AnonymousAnswer.objects.filter(session_key = session_key)
        #from askbot.conf import settings as askbot_settings
        if askbot_settings.EMAIL_VALIDATION == True:#add user to the record
            for aq in aq_list:
                aq.author = self.user
                aq.save()
            for aa in aa_list:
                aa.author = self.user
                aa.save()
            #maybe add pending posts message?
        else:
            if self.user.is_blocked():
                msg = get_i18n_message('BLOCKED_USERS_CANNOT_POST')
                self.user.message_set.create(message = msg)
            elif self.user.is_suspended():
                msg = get_i18n_message('SUSPENDED_USERS_CANNOT_POST')
                self.user.message_set.create(message = msg)
            else:
                for aq in aq_list:
                    aq.publish(self.user)
                for aa in aa_list:
                    aa.publish(self.user)
    
    
    def mark_tags(
                self,
                tagnames = None,
                wildcards = None,
                reason = None,
                action = None
            ):
        """subscribe for or ignore a list of tags
    
        * ``tagnames`` and ``wildcards`` are lists of
          pure tags and wildcard tags, respectively
        * ``reason`` - either "good" or "bad"
        * ``action`` - eitrer "add" or "remove"
        """
        cleaned_wildcards = list()
        assert(action in ('add', 'remove'))
        if action == 'add':
            if askbot_settings.SUBSCRIBED_TAG_SELECTOR_ENABLED:
                assert(reason in ('good', 'bad', 'subscribed'))
            else:
                assert(reason in ('good', 'bad'))
        if wildcards:
            cleaned_wildcards = self.update_wildcard_tag_selections(
                action = action,
                reason = reason,
                wildcards = wildcards
            )
        if tagnames is None:
            tagnames = list()
    
        #below we update normal tag selections
        marked_ts = MarkedTag.objects.filter(
                                        user = self.user,
                                        tag__name__in = tagnames
                                    )
        #Marks for "good" and "bad" reasons are exclusive,
        #to make it impossible to "like" and "dislike" something at the same time
        #but the subscribed set is independent - e.g. you can dislike a topic
        #and still subscribe for it.
        if reason == 'subscribed':
            #don't touch good/bad marks
            marked_ts = marked_ts.filter(reason = 'subscribed')
        else:
            #and in this case don't touch subscribed tags
            marked_ts = marked_ts.exclude(reason = 'subscribed')
    
        #todo: use the user api methods here instead of the straight ORM
        cleaned_tagnames = list() #those that were actually updated
        if action == 'remove':
            logging.debug('deleting tag marks: %s' % ','.join(tagnames))
            marked_ts.delete()
        else:
            marked_names = marked_ts.values_list('tag__name', flat = True)
            if len(marked_names) < len(tagnames):
                unmarked_names = set(tagnames).difference(set(marked_names))
                ts = Tag.objects.filter(name__in = unmarked_names)
                new_marks = list()
                for tag in ts:
                    MarkedTag(
                        user = self.user,
                        reason = reason,
                        tag = tag
                    ).save()
                    new_marks.append(tag.name)
                cleaned_tagnames.extend(marked_names)
                cleaned_tagnames.extend(new_marks)
            else:
                if reason in ('good', 'bad'):#to maintain exclusivity of 'good' and 'bad'
                    marked_ts.update(reason=reason)
                cleaned_tagnames = tagnames
    
        return cleaned_tagnames, cleaned_wildcards
    
    @auto_now_timestamp
    def retag_question(
                        self,
                        question = None,
                        tags = None,
                        timestamp = None,
                        silent = False
                    ):
        self.assert_can_retag_question(question)
        question.thread.retag(
            retagged_by = self,
            retagged_at = timestamp,
            tagnames = tags,
            silent = silent
        )
        question.thread.invalidate_cached_data()
        award_badges_signal.send(None,
            event = 'retag_question',
            actor = self,
            context_object = question,
            timestamp = timestamp
        )
    
    @auto_now_timestamp
    def accept_best_answer(
                    self, answer = None,
                    timestamp = None,
                    cancel = False,
                    force = False
                ):
        if cancel:
            return self.unaccept_best_answer(
                                    answer = answer,
                                    timestamp = timestamp,
                                    force = force
                                )
        if force == False:
            self.assert_can_accept_best_answer(answer)
        if answer.accepted() == True:
            return
    
        prev_accepted_answer = answer.thread.accepted_answer
        if prev_accepted_answer:
            auth.onAnswerAcceptCanceled(prev_accepted_answer, self)
    
        auth.onAnswerAccept(answer, self, timestamp = timestamp)
        award_badges_signal.send(None,
            event = 'accept_best_answer',
            actor = self,
            context_object = answer,
            timestamp = timestamp
        )
    
    @auto_now_timestamp
    def unaccept_best_answer(
                    self, answer = None,
                    timestamp = None,
                    force = False
                ):
        if force == False:
            self.assert_can_unaccept_best_answer(answer)
        if not answer.accepted():
            return
        auth.onAnswerAcceptCanceled(answer, self)
    
    @auto_now_timestamp
    def delete_comment(
                        self,
                        comment = None,
                        timestamp = None
                    ):
        self.assert_can_delete_comment(comment = comment)
        #todo: we want to do this
        #comment.deleted = True
        #comment.deleted_by = self
        #comment.deleted_at = timestamp
        #comment.save()
        comment.delete()
        comment.thread.invalidate_cached_data()
    
    @auto_now_timestamp
    def delete_answer(
                        self,
                        answer = None,
                        timestamp = None
                    ):
        self.assert_can_delete_answer(answer = answer)
        answer.deleted = True
        answer.deleted_by = self
        answer.deleted_at = timestamp
        answer.save()
    
        answer.thread.update_answer_count()
        answer.thread.invalidate_cached_data()
        logging.debug('updated answer count to %d' % answer.thread.answer_count)
    
        signals.delete_question_or_answer.send(
            sender = answer.__class__,
            instance = answer,
            delete_by = self
        )
        award_badges_signal.send(None,
                    event = 'delete_post',
                    actor = self,
                    context_object = answer,
                    timestamp = timestamp
                )
    
    
    @auto_now_timestamp
    def delete_question(
                        self,
                        question = None,
                        timestamp = None
                    ):
        self.assert_can_delete_question(question = question)
    
        question.deleted = True
        question.deleted_by = self
        question.deleted_at = timestamp
        question.save()
    
        for tag in list(question.thread.tags.all()):
            if tag.used_count == 1:
                tag.deleted = True
                tag.deleted_by = self
                tag.deleted_at = timestamp
            else:
                tag.used_count = tag.used_count - 1
            tag.save()
    
        signals.delete_question_or_answer.send(
            sender = question.__class__,
            instance = question,
            delete_by = self
        )
        award_badges_signal.send(None,
                    event = 'delete_post',
                    actor = self,
                    context_object = question,
                    timestamp = timestamp
                )
    
    
    @auto_now_timestamp
    def close_question(
                        self,
                        question = None,
                        reason = None,
                        timestamp = None
                    ):
        self.assert_can_close_question(question)
        question.thread.set_closed_status(closed=True, closed_by=self, closed_at=timestamp, close_reason=reason)
    
    @auto_now_timestamp
    def reopen_question(
                        self,
                        question = None,
                        timestamp = None
                    ):
        self.assert_can_reopen_question(question)
        question.thread.set_closed_status(closed=False, closed_by=self, closed_at=timestamp, close_reason=None)
    
    @auto_now_timestamp
    def delete_post(
                        self,
                        post = None,
                        timestamp = None
                    ):
        """generic delete method for all kinds of posts
    
        if there is no use cases for it, the method will be removed
        """
        if post.post_type == 'comment':
            self.delete_comment(comment = post, timestamp = timestamp)
        elif post.post_type == 'answer':
            self.delete_answer(answer = post, timestamp = timestamp)
        elif post.post_type == 'question':
            self.delete_question(question = post, timestamp = timestamp)
        else:
            raise TypeError('either Comment, Question or Answer expected')
        post.thread.invalidate_cached_data()
    
    def restore_post(
                        self,
                        post = None,
                        timestamp = None
                    ):
        #here timestamp is not used, I guess added for consistency
        self.assert_can_restore_post(post)
        if post.post_type in ('question', 'answer'):
            post.deleted = False
            post.deleted_by = None
            post.deleted_at = None
            post.save()
            post.thread.invalidate_cached_data()
            if post.post_type == 'answer':
                post.thread.update_answer_count()
            else:
                #todo: make sure that these tags actually exist
                #some may have since been deleted for good
                #or merged into others
                for tag in list(post.thread.tags.all()):
                    if tag.used_count == 1 and tag.deleted:
                        tag.deleted = False
                        tag.deleted_by = None
                        tag.deleted_at = None
                        tag.save()
        else:
            raise NotImplementedError()
    
    def post_question(self,
                        language_code,
                        site,
                        title = None,
                        body_text = '',
                        tags = None,
                        wiki = False,
                        is_anonymous = False,
                        timestamp = None,
                        by_email = False,
                        email_address = None):
        """makes an assertion whether user can post the question
        then posts it and returns the question object"""
    
        self.assert_can_post_question()
    
        if body_text == '':#a hack to allow bodyless question
            body_text = ' '
    
        if title is None:
            raise ValueError('Title is required to post question')
        if tags is None:
            raise ValueError('Tags are required to post question')
        if timestamp is None:
            timestamp = datetime.datetime.now()
    
        #todo: split this into "create thread" + "add queston", if text exists
        #or maybe just add a blank question post anyway
        thread = Thread.objects.create_new(
                                        language_code = language_code,
                                        site=site,
                                        author = self.user,
                                        title = title,
                                        text = body_text,
                                        tagnames = tags,
                                        added_at = timestamp,
                                    wiki = wiki,
                                    is_anonymous = is_anonymous,
                                    by_email = by_email,
                                    email_address = email_address
                                )
        question = thread._question_post()
        if question.author != self.user:
            raise ValueError('question.author != self.user')
        question.author = self.user # HACK: Some tests require that question.author IS exactly the same object as self-user (kind of identity map which Django doesn't provide),
                               #       because they set some attributes for that instance and expect them to be changed also for question.author
        return question
    
    @auto_now_timestamp
    def edit_comment(
                        self,
                        comment_post=None,
                        body_text = None,
                        timestamp = None,
                        by_email = False
                    ):
        """apply edit to a comment, the method does not
        change the comments timestamp and no signals are sent
        todo: see how this can be merged with edit_post
        todo: add timestamp
        """
        self.assert_can_edit_comment(comment_post)
        comment_post.apply_edit(
                            text = body_text,
                            edited_at = timestamp,
                            edited_by = self,
                            by_email = by_email
                        )
        comment_post.thread.invalidate_cached_data()
    
    def edit_post(self,
                    post = None,
                    body_text = None,
                    revision_comment = None,
                    timestamp = None,
                    by_email = False
                ):
        """a simple method that edits post body
        todo: unify it in the style of just a generic post
        this requires refactoring of underlying functions
        because we cannot bypass the permissions checks set within
        """
        if post.post_type == 'comment':
            self.edit_comment(
                    comment_post = post,
                    body_text = body_text,
                    by_email = by_email
                )
        elif post.post_type == 'answer':
            self.edit_answer(
                answer = post,
                body_text = body_text,
                timestamp = timestamp,
                revision_comment = revision_comment,
                by_email = by_email
            )
        elif post.post_type == 'question':
            self.edit_question(
                question = post,
                body_text = body_text,
                timestamp = timestamp,
                revision_comment = revision_comment,
                by_email = by_email
            )
        elif post.post_type == 'tag_wiki':
            post.apply_edit(
                edited_at = timestamp,
                edited_by = self,
                text = body_text,
                #todo: summary name clash in question and question revision
                comment = revision_comment,
                wiki = True,
                by_email = False
            )
        else:
            raise NotImplementedError()
    
    @auto_now_timestamp
    def edit_question(
                    self,
                    question = None,
                    title = None,
                    body_text = None,
                    revision_comment = None,
                    tags = None,
                    wiki = False,
                    edit_anonymously = False,
                    timestamp = None,
                    force = False,#if True - bypass the assert
                    by_email = False
                ):
        if force == False:
            self.assert_can_edit_question(question)
    
        question.apply_edit(
            edited_at = timestamp,
            edited_by = self,
            title = title,
            text = body_text,
            #todo: summary name clash in question and question revision
            comment = revision_comment,
            tags = tags,
            wiki = wiki,
            edit_anonymously = edit_anonymously,
            by_email = by_email
        )
    
        question.thread.invalidate_cached_data()
    
        award_badges_signal.send(None,
            event = 'edit_question',
            actor = self,
            context_object = question,
            timestamp = timestamp
        )
    
    @auto_now_timestamp
    def edit_answer(
                        self,
                        answer = None,
                        body_text = None,
                        revision_comment = None,
                        wiki = False,
                        timestamp = None,
                        force = False,#if True - bypass the assert
                        by_email = False
                    ):
        if force == False:
            self.assert_can_edit_answer(answer)
        answer.apply_edit(
            edited_at = timestamp,
            edited_by = self,
            text = body_text,
            comment = revision_comment,
            wiki = wiki,
            by_email = by_email
        )
        answer.thread.invalidate_cached_data()
        award_badges_signal.send(None,
            event = 'edit_answer',
            actor = self,
            context_object = answer,
            timestamp = timestamp
        )
    
    @auto_now_timestamp
    def create_post_reject_reason(
        self, title = None, details = None, timestamp = None
    ):
        """creates and returs the post reject reason"""
        reason = PostFlagReason(
            title = title,
            added_at = timestamp,
            author = self.user
        )
    
        #todo - need post_object.create_new() method
        details = Post(
            post_type = 'reject_reason',
            author = self.user,
            added_at = timestamp,
            text = details
        )
        details.parse_and_save(author = self.user)
        details.add_revision(
            author = self.user,
            revised_at = timestamp,
            text = details,
            comment = const.POST_STATUS['default_version']
        )
    
        reason.details = details
        reason.save()
        return reason
    
    @auto_now_timestamp
    def edit_post_reject_reason(
        self, reason, title = None, details = None, timestamp = None
    ):
        reason.title = title
        reason.save()
        reason.details.apply_edit(
            edited_by = self,
            edited_at = timestamp,
            text = details
        )
    
    def post_answer(
                        self,
                        question = None,
                        body_text = None,
                        follow = False,
                        wiki = False,
                        timestamp = None,
                        by_email = False
                    ):
    
        #todo: move this to assertion - user_assert_can_post_answer
        if self.user == question.author and not self.is_administrator():
    
            # check date and rep required to post answer to own question
    
            delta = datetime.timedelta(askbot_settings.MIN_DAYS_TO_ANSWER_OWN_QUESTION)
    
            now = datetime.datetime.now()
            asked = question.added_at
            #todo: this is an assertion, must be moved out
            if (now - asked  < delta and self.reputation < askbot_settings.MIN_REP_TO_ANSWER_OWN_QUESTION):
                diff = asked + delta - now
                days = diff.days
                hours = int(diff.seconds/3600)
                minutes = int(diff.seconds/60)
    
                if days > 2:
                    if asked.year == now.year:
                        date_token = asked.strftime("%b %d")
                    else:
                        date_token = asked.strftime("%b %d '%y")
                    left = _('on %(date)s') % { 'date': date_token }
                elif days == 2:
                    left = _('in two days')
                elif days == 1:
                    left = _('tomorrow')
                elif minutes >= 60:
                    left = ungettext('in %(hr)d hour','in %(hr)d hours',hours) % {'hr':hours}
                else:
                    left = ungettext('in %(min)d min','in %(min)d mins',minutes) % {'min':minutes}
                day = ungettext('%(days)d day','%(days)d days',askbot_settings.MIN_DAYS_TO_ANSWER_OWN_QUESTION) % {'days':askbot_settings.MIN_DAYS_TO_ANSWER_OWN_QUESTION}
                error_message = _(
                    'New users must wait %(days)s before answering their own question. '
                    ' You can post an answer %(left)s'
                    ) % {'days': day,'left': left}
                assert(error_message is not None)
                raise django_exceptions.PermissionDenied(error_message)
    
        self.assert_can_post_answer(thread = question.thread)
    
        if getattr(question, 'post_type', '') != 'question':
            raise TypeError('question argument must be provided')
        if body_text is None:
            raise ValueError('Body text is required to post answer')
        if timestamp is None:
            timestamp = datetime.datetime.now()
    #    answer = Answer.objects.create_new(
    #        thread = question.thread,
    #        author = self,
    #        text = body_text,
    #        added_at = timestamp,
    #        email_notify = follow,
    #        wiki = wiki
    #    )
        answer_post = Post.objects.create_new_answer(
            thread = question.thread,
            author = self.user,
            text = body_text,
            added_at = timestamp,
            email_notify = follow,
            wiki = wiki,
            by_email = by_email
        )
        answer_post.thread.invalidate_cached_data()
        award_badges_signal.send(None,
            event = 'post_answer',
            actor = self.user,
            context_object = answer_post
        )
        return answer_post
    
    def visit_question(self, question = None, timestamp = None):
        """create a QuestionView record
        on behalf of the user represented by the self object
        and mark it as taking place at timestamp time
    
        and remove pending on-screen notifications about anything in
        the post - question, answer or comments
        """
        if timestamp is None:
            timestamp = datetime.datetime.now()
    
        try:
            QuestionView.objects.filter(
                who=self, question=question
            ).update(
                when = timestamp
            )
        except QuestionView.DoesNotExist:
            QuestionView(
                who=self,
                question=question,
                when = timestamp
            ).save()
    
        #filter memo objects on response activities directed to the qurrent user
        #that refer to the children of the currently
        #viewed question and clear them for the current user
        ACTIVITY_TYPES = const.RESPONSE_ACTIVITY_TYPES_FOR_DISPLAY
        ACTIVITY_TYPES += (const.TYPE_ACTIVITY_MENTION,)
    
        audit_records = ActivityAuditStatus.objects.filter(
                            user = self,
                            status = ActivityAuditStatus.STATUS_NEW,
                            activity__question = question
                        )
    
        cleared_record_count = audit_records.filter(
                                    activity__activity_type__in = ACTIVITY_TYPES
                                ).update(
                                    status=ActivityAuditStatus.STATUS_SEEN
                                )
        if cleared_record_count > 0:
            self.update_response_counts()
    
        #finally, mark admin memo objects if applicable
        #the admin response counts are not denormalized b/c they are easy to obtain
        if self.is_moderator() or self.is_administrator():
            audit_records.filter(
                    activity__activity_type = const.TYPE_ACTIVITY_MARK_OFFENSIVE
            ).update(
                status=ActivityAuditStatus.STATUS_SEEN
            )
            
    def has_interesting_wildcard_tags(self):
        """True in wildcard tags aro on and
        user has nome interesting wildcard tags selected
        """
        return (
            askbot_settings.USE_WILDCARD_TAGS \
            and self.interesting_tags != ''
        )
    
    def can_have_strong_url(self):
        """True if user's homepage url can be
        followed by the search engine crawlers"""
        return (self.reputation >= askbot_settings.MIN_REP_TO_HAVE_STRONG_URL)
    
    def can_post_by_email(self):
        """True, if reply by email is enabled 
        and user has sufficient reputatiton"""
        return askbot_settings.REPLY_BY_EMAIL and \
            self.reputation > askbot_settings.MIN_REP_TO_POST_BY_EMAIL
    
    def get_or_create_fake_user(self, username, email):
        """
        Get's or creates a user, most likely with the purpose
        of posting under that account.
        """
        assert(self.is_administrator())
    
        try:
            user = User.objects.get(username=username)
        except User.DoesNotExist:
            user = User()
            user.username = username
            user.email = email
            user.set_unusable_password()
            user.save()
            self.objects.create(user=user, is_fake=True)
        return user
    
    def is_administrator(self):
        """checks whether user in the forum site administrator
        the admin must be both superuser and staff member
        the latter is because staff membership is required
        to access the live settings"""
        return (self.user.is_superuser and self.user.is_staff)
    
    def remove_admin_status(self):
        self.user.is_staff = False
        self.user.is_superuser = False
    
    def set_admin_status(self):
        self.user.is_staff = True
        self.user.is_superuser = True
    
    def add_missing_askbot_subscriptions(self):
        from askbot import forms#need to avoid circular dependency
        form = forms.EditUserEmailFeedsForm()
        need_feed_types = form.get_db_model_subscription_type_names()
        have_feed_types = EmailFeedSetting.objects.filter(
                                                subscriber = self.user
                                            ).values_list(
                                                'feed_type', flat = True
                                            )
        missing_feed_types = set(need_feed_types) - set(have_feed_types)
        for missing_feed_type in missing_feed_types:
            attr_key = 'DEFAULT_NOTIFICATION_DELIVERY_SCHEDULE_%s' % missing_feed_type.upper()
            freq = getattr(askbot_settings, attr_key)
            feed_setting = EmailFeedSetting(
                                subscriber = self.user,
                                feed_type = missing_feed_type,
                                frequency = freq
                            )
            feed_setting.save()
    
    def is_moderator(self):
        return (self.status == 'm' and self.is_administrator() == False)
    
    def is_administrator_or_moderator(self):
        return (self.is_administrator() or self.is_moderator())
    
    def is_suspended(self):
        return (self.status == 's')
    
    def is_blocked(self):
        return (self.status == 'b')
    
    def is_watched(self):
        return (self.status == 'w')
    
    def is_approved(self):
        return (self.status == 'a')
    
    def is_owner_of(self, obj):
        """True if user owns object
        False otherwise
        """
        if isinstance(obj, Post) and obj.post_type == 'question':
            return self.user == obj.author
        else:
            raise NotImplementedError()

    
    def get_anonymous_name(self):
        """Returns name of anonymous user
        - convinience method for use in the template
        macros that accept user as parameter
        """
        return get_name_of_anonymous_user()
    
    def set_status(self, new_status):
        """sets new status to user
    
        this method understands that administrator status is
        stored in the User.is_superuser field, but
        everything else in User.status field
    
        there is a slight aberration - administrator status
        can be removed, but not added yet
    
        if new status is applied to user, then the record is
        committed to the database
        """
        #d - administrator
        #m - moderator
        #s - suspended
        #b - blocked
        #w - watched
        #a - approved (regular user)
        assert(new_status in ('d', 'm', 's', 'b', 'w', 'a'))
        if new_status == self.status:
            return
    
        #clear admin status if user was an administrator
        #because this function is not dealing with the site admins
    
        if new_status == 'd':
            #create a new admin
            self.set_admin_status()
        else:
            #This was the old method, kept in the else clause when changing
            #to admin, so if you change the status to another thing that
            #is not Administrator it will simply remove admin if the user have
            #that permission, it will mostly be false.
            if self.is_administrator():
                self.remove_admin_status()
    
        #when toggling between blocked and non-blocked status
        #we need to invalidate question page caches, b/c they contain
        #user's url, which must be hidden in the blocked state
        if 'b' in (new_status, self.status) and new_status != self.status:
            threads = Thread.objects.get_for_user(self)
            for thread in threads:
                thread.invalidate_cached_post_data()
    
        self.status = new_status
        self.save()
    
    @auto_now_timestamp
    def moderate_user_reputation(
                                    self,
                                    user = None,
                                    reputation_change = 0,
                                    comment = None,
                                    timestamp = None
                                ):
        """add or subtract reputation of other user
        """
        if reputation_change == 0:
            return
        if comment == None:
            raise ValueError('comment is required to moderate user reputation')
    
        new_rep = user.reputation + reputation_change
        if new_rep < 1:
            new_rep = 1 #todo: magic number
            reputation_change = 1 - user.reputation
    
        user.reputation = new_rep
        user.save()
    
        #any question. This is necessary because reputes are read in the
        #user_reputation view with select_related('question__title') and it fails if
        #ForeignKey is nullable even though it should work (according to the manual)
        #probably a bug in the Django ORM
        #fake_question = Question.objects.all()[:1][0]
        #so in cases where reputation_type == 10
        #question record is fake and is ignored
        #this bug is hidden in call Repute.get_explanation_snippet()
        repute = Repute(
                            user = user,
                            comment = comment,
                            #question = fake_question,
                            reputed_at = timestamp,
                            reputation_type = 10, #todo: fix magic number
                            reputation = user.reputation
                        )
        if reputation_change < 0:
            repute.negative = -1 * reputation_change
        else:
            repute.positive = reputation_change
        repute.save()
    
    def get_status_display(self, soft = False):
        if self.is_administrator():
            return _('Site Adminstrator')
        elif self.is_moderator():
            return _('Forum Moderator')
        elif self.is_suspended():
            return  _('Suspended User')
        elif self.is_blocked():
            return _('Blocked User')
        elif soft == True:
            return _('Registered User')
        elif self.is_watched():
            return _('Watched User')
        elif self.is_approved():
            return _('Approved User')
        else:
            raise ValueError('Unknown user status')
    
    
    def can_moderate_user(self, other):
        if self.is_administrator():
            return True
        elif self.is_moderator():
            if other.is_moderator() or other.is_administrator():
                return False
            else:
                return True
        else:
            return False
    
    
    def get_followed_question_alert_frequency(self):
        feed_setting, created = EmailFeedSetting.objects.get_or_create(
                                        subscriber=self,
                                        feed_type='q_sel'
                                    )
        return feed_setting.frequency
    
    def subscribe_for_followed_question_alerts(self):
        """turns on daily subscription for selected questions
        otherwise does nothing
    
        Returns ``True`` if the subscription was turned on and
        ``False`` otherwise
        """
        feed_setting, created = EmailFeedSetting.objects.get_or_create(
                                                            subscriber = self,
                                                            feed_type = 'q_sel'
                                                        )
        if feed_setting.frequency == 'n':
            feed_setting.frequency = 'd'
            feed_setting.save()
            return True
        return False
    
    def get_tag_filtered_questions(self, questions = None):
        """Returns a query set of questions, tag filtered according
        to the user choices. Parameter ``questions`` can be either ``None``
        or a starting query set.
        """
        if questions is None:
            questions = Post.objects.get_questions()
    
        if self.email_tag_filter_strategy == const.EXCLUDE_IGNORED:
    
            ignored_tags = Tag.objects.filter(
                                    user_selections__reason = 'bad',
                                    user_selections__user = self
                                )
    
            wk = self.ignored_tags.strip().split()
            ignored_by_wildcards = Tag.objects.get_by_wildcards(wk)
    
            return questions.exclude(
                            thread__tags__in = ignored_tags
                        ).exclude(
                            thread__tags__in = ignored_by_wildcards
                        ).distinct()
        elif self.email_tag_filter_strategy == const.INCLUDE_INTERESTING:
            if askbot_settings.SUBSCRIBED_TAG_SELECTOR_ENABLED:
                reason = 'subscribed'
                wk = self.subscribed_tags.strip().split()
            else:
                reason = 'good'
                wk = self.interesting_tags.strip().split()
    
            selected_tags = Tag.objects.filter(
                                    user_selections__reason = reason,
                                    user_selections__user = self
                                )
    
            selected_by_wildcards = Tag.objects.get_by_wildcards(wk)
    
            tag_filter = models.Q(thread__tags__in = list(selected_tags)) \
                        | models.Q(thread__tags__in = list(selected_by_wildcards))
    
            return questions.filter( tag_filter ).distinct()
        else:
            return questions
    
    def get_messages(self):
        messages = []
        for m in self.message_set.all():
            messages.append(m.message)
        return messages
    
    def delete_messages(self):
        self.message_set.all().delete()
    
    #todo: find where this is used and replace with get_absolute_url
    def get_profile_url(self):
        return self.get_absolute_url()
    
    def get_absolute_url(self):
        raise "Must be implemented by concrete profile"
    
    def get_groups_membership_info(self, groups):
        """returts a defaultdict with values that are
        dictionaries with the following keys and values:
        * key: can_join, value: True if user can join group
        * key: is_member, value: True if user is member of group
    
        ``groups`` is a group tag query set
        """
        groups = groups.select_related('group_profile')
    
        group_ids = groups.values_list('id', flat = True)
        memberships = GroupMembership.objects.filter(
                                    user__id = self.id,
                                    group__id__in = group_ids
                                )
    
        info = collections.defaultdict(
            lambda: {'can_join': False, 'is_member': False}
        )
        for membership in memberships:
            info[membership.group_id]['is_member'] = True
    
        for group in groups:
            info[group.id]['can_join'] = group.group_profile.can_accept_user(self)
    
        return info
            
    
    
    def get_karma_summary(self):
        """returns human readable sentence about
        status of user's karma"""
        return _("%(username)s karma is %(reputation)s") % \
                {'username': self.user.username, 'reputation': self.reputation}
    
    def get_badge_summary(self):
        """returns human readable sentence about
        number of badges of different levels earned
        by the user. It is assumed that user has some badges"""
        badge_bits = list()
        if self.gold:
            bit = ungettext(
                    'one gold badge',
                    '%(count)d gold badges',
                    self.gold
                ) % {'count': self.gold}
            badge_bits.append(bit)
        if self.silver:
            bit = ungettext(
                    'one silver badge',
                    '%(count)d silver badges',
                    self.gold
                ) % {'count': self.silver}
            badge_bits.append(bit)
        if self.silver:
            bit = ungettext(
                    'one bronze badge',
                    '%(count)d bronze badges',
                    self.gold
                ) % {'count': self.bronze}
            badge_bits.append(bit)
    
        if len(badge_bits) == 1:
            badge_str = badge_bits[0]
        elif len(badge_bits) > 1:
            last_bit = badge_bits.pop()
            badge_str = ', '.join(badge_bits)
            badge_str = _('%(item1)s and %(item2)s') % \
                        {'item1': badge_str, 'item2': last_bit}
        else:
            raise ValueError('user must have badges to call this function')
        return _("%(user)s has %(badges)s") % {'user': self.username, 'badges':badge_str}
    
    #series of methods for user vote-type commands
    #same call signature func(self, post, timestamp=None, cancel=None)
    #note that none of these have business logic checks internally
    #these functions are used by the askbot app and
    #by the data importer jobs from say stackexchange, where internal rules
    #may be different
    #maybe if we do use business rule checks here - we should add
    #some flag allowing to bypass them for things like the data importers
    def toggle_favorite_question(
                            self, question,
                            timestamp = None,
                            cancel = False,
                            force = False#this parameter is not used yet
                        ):
        """cancel has no effect here, but is important for the SE loader
        it is hoped that toggle will work and data will be consistent
        but there is no guarantee, maybe it's better to be more strict
        about processing the "cancel" option
        another strange thing is that this function unlike others below
        returns a value
        """
        try:
            fave = FavoriteQuestion.objects.get(thread=question.thread, user=self)
            fave.delete()
            result = False
            question.thread.update_favorite_count()
        except FavoriteQuestion.DoesNotExist:
            if timestamp is None:
                timestamp = datetime.datetime.now()
            fave = FavoriteQuestion(
                thread = question.thread,
                user = self,
                added_at = timestamp,
            )
            fave.save()
            result = True
            question.thread.update_favorite_count()
            award_badges_signal.send(None,
                event = 'select_favorite_question',
                actor = self,
                context_object = question,
                timestamp = timestamp
            )
        return result

    
    
    def unfollow_question(self, question = None):
        self.followed_threads.remove(question.thread)
    
    def follow_question(self, question = None):
        self.followed_threads.add(question.thread)
    
    def is_following_question(self, question):
        """True if user is following a question"""
        return question.thread.followed_by.filter(id=self.user.id).exists()
    
    
    def upvote(self, post, timestamp=None, cancel=False, force = False):
        #force parameter not used yet
        return _process_vote(
            self,
            post,
            timestamp=timestamp,
            cancel=cancel,
            vote_type=Vote.VOTE_UP
        )
    
    def downvote(self, post, timestamp=None, cancel=False, force = False):
        #force not used yet
        return _process_vote(
            self,
            post,
            timestamp=timestamp,
            cancel=cancel,
            vote_type=Vote.VOTE_DOWN
        )
    
    @auto_now_timestamp
    def approve_post_revision(self, post_revision, timestamp = None):
        """approves the post revision and, if necessary,
        the parent post and threads"""
        self.user.assert_can_approve_post_revision()
    
        post_revision.approved = True
        post_revision.approved_by = self.user
        post_revision.approved_at = timestamp
    
        post_revision.save()
    
        post = post_revision.post
        post.approved = True
        post.save()
    
        if post_revision.post.post_type == 'question':
            thread = post.thread
            thread.approved = True
            thread.save()
        post.thread.invalidate_cached_data()
    
        #send the signal of published revision
        signals.post_revision_published.send(
            None, revision = post_revision, was_approved = True
        )
    
    @auto_now_timestamp
    def flag_post(self, post, timestamp=None, cancel=False, cancel_all = False, force = False):
        if cancel_all:
            # remove all flags
            if force == False:
                self.user.assert_can_remove_all_flags_offensive(post = post)
            post_content_type = ContentType.objects.get_for_model(post)
            all_flags = Activity.objects.filter(
                            activity_type = const.TYPE_ACTIVITY_MARK_OFFENSIVE,
                            content_type = post_content_type, object_id=post.id
                        )
            for flag in all_flags:
                auth.onUnFlaggedItem(post, flag.user, timestamp=timestamp)            
    
        elif cancel:#todo: can't unflag?
            if force == False:
                self.user.assert_can_remove_flag_offensive(post = post)
            auth.onUnFlaggedItem(post, self.user, timestamp=timestamp)        
    
        else:
            if force == False:
                self.user.assert_can_flag_offensive(post = post)
            auth.onFlaggedItem(post, self.user, timestamp=timestamp)
            award_badges_signal.send(None,
                event = 'flag_post',
                actor = self.user,
                context_object = post,
                timestamp = timestamp
            )
    
    def get_flags(self):
        """return flag Activity query set
        for all flags set by te user"""
        return Activity.objects.filter(
                            user = self,
                            activity_type = const.TYPE_ACTIVITY_MARK_OFFENSIVE
                        )
    
    def get_flag_count_posted_today(self):
        """return number of flags the user has posted
        within last 24 hours"""
        today = datetime.date.today()
        time_frame = (today, today + datetime.timedelta(1))
        flags = self.get_flags()
        return flags.filter(active_at__range = time_frame).count()
    
    def get_flags_for_post(self, post):
        """return query set for flag Activity items
        posted by users for a given post obeject
        """
        post_content_type = ContentType.objects.get_for_model(post)
        flags = self.get_flags()
        return flags.filter(content_type = post_content_type, object_id=post.id)
    
    def update_response_counts(self):
        """Recount number of responses to the user.
        """
        ACTIVITY_TYPES = const.RESPONSE_ACTIVITY_TYPES_FOR_DISPLAY
        ACTIVITY_TYPES += (const.TYPE_ACTIVITY_MENTION,)
    
        self.user.new_response_count = ActivityAuditStatus.objects.filter(
                                        user = self.user,
                                        status = ActivityAuditStatus.STATUS_NEW,
                                        activity__activity_type__in = ACTIVITY_TYPES
                                    ).count()
        self.user.seen_response_count = ActivityAuditStatus.objects.filter(
                                        user = self.user,
                                        status = ActivityAuditStatus.STATUS_SEEN,
                                        activity__activity_type__in = ACTIVITY_TYPES
                                    ).count()
        self.user.save()
    
    
    def receive_reputation(self, num_points):
        new_points = self.reputation + num_points
        if new_points > 0:
            self.reputation = new_points
        else:
            self.reputation = const.MIN_REPUTATION
    
    def update_wildcard_tag_selections(
                                        self,
                                        action = None,
                                        reason = None,
                                        wildcards = None,
                                    ):
        """updates the user selection of wildcard tags
        and saves the user object to the database
        """
        if askbot_settings.SUBSCRIBED_TAG_SELECTOR_ENABLED:
            assert reason in ('good', 'bad', 'subscribed')
        else:
            assert reason in ('good', 'bad')
    
        new_tags = set(wildcards)
        interesting = set(self.interesting_tags.split())
        ignored = set(self.ignored_tags.split())
        subscribed = set(self.subscribed_tags.split())
    
        if reason == 'good':
            target_set = interesting
            other_set = ignored
        elif reason == 'bad':
            target_set = ignored
            other_set = interesting
        elif reason == 'subscribed':
            target_set = subscribed
            other_set = None
        else:
            assert(action == 'remove')
    
        if action == 'add':
            target_set.update(new_tags)
            if reason in ('good', 'bad'):
                other_set.difference_update(new_tags)
        else:
            target_set.difference_update(new_tags)
            if reason in ('good', 'bad'):
                other_set.difference_update(new_tags)
    
        self.interesting_tags = ' '.join(interesting)
        self.ignored_tags = ' '.join(ignored)
        self.subscribed_tags = ' '.join(subscribed)
        self.save()
        return new_tags
    
    
    def edit_group_membership(self, user = None, group = None, action = None):
        """allows one user to add another to a group
        or remove user from group.
    
        If when adding, the group does not exist, it will be created
        the delete function is not symmetric, the group will remain
        even if it becomes empty
        """
        if action == 'add':
            GroupMembership.objects.get_or_create(user = user, group = group)
        elif action == 'remove':
            GroupMembership.objects.get(user = user, group = group).delete()
        else:
            raise ValueError('invalid action')
    
    def is_group_member(self, group = None):
        return self.group_memberships.filter(group = group).count() == 1

    class Meta:
        abstract = True

class AskbotProfile(AskbotBaseProfile, UserenaLanguageBaseProfile):
    """
    Profile model example
    """
    user = models.OneToOneField(User)
    
#    email_isvalid = models.BooleanField(default=False)
#    email_key = models.CharField(max_length=32, null=True)
#    gravatar = models.CharField(max_length=32)
#    avatar_type = models.CharField(max_length=1, choices=const.AVATAR_STATUS_CHOICE, default='n')
    real_name = models.CharField(max_length=100, blank=True)
    website = models.URLField(max_length=200, blank=True)
    location = models.CharField(max_length=100, blank=True)
    country = CountryField(blank = True)
    show_country = models.BooleanField(default = False)
    date_of_birth = models.DateField(null=True, blank=True)
    about = models.TextField(blank=True)
    
    class Meta:
        app_label = 'askbot'
        
    def __getattr__(self, name):
        info("getattr %s" % name)
        if name in [f.name for f in User._meta.fields if f.name != 'id']:
            return object.__getattribute__(self.user, name)
        
        raise AttributeError
    
    def __setattr__(self, name, value):
        if name in [f.name for f in User._meta.fields if f.name != 'id']:
            info("setattr %s" % name)
            object.__setattr__(self.user, name, value)
        else:
            object.__setattr__(self, name, value)
    
    """Returns the URL for this User's profile."""
    def get_absolute_url(self):
        return reverse('user_profile', kwargs={'id':self.user.id, 
                                               'slug':slugify(self.user.username)})
    


def create_user_profile(sender, instance, created, **kwargs):
    if created:
        AskbotProfile.objects.create(user=instance)

def propragate_user_save(sender, instance, created, **kwargs):
    """
    Temporary callback due to the user profile refactoring
    """
    if not created:
        if hasattr(instance, 'profile_must_be_saved') and instance.profile_must_be_saved:
            instance.get_profile().save()
            instance.profile_must_be_save = False

if django_settings.AUTH_PROFILE_MODULE == "askbot.AskbotProfile":
    django_signals.post_save.connect(create_user_profile, sender=User)
    
    
if django_settings.AUTH_PROFILE_MODULE != "auth.User":
    django_signals.post_save.connect(propragate_user_save, sender=User)