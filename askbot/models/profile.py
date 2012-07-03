from django.db import models
from django.conf import settings as django_settings
from django.db.models import signals as django_signals
from django.contrib.auth.models import User

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

class AskbotProfile(models.Model):
    """
    Profile model example
    """
    user = models.OneToOneField(User)
    
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
    
    @classmethod
    def add_to_class(cls, name, value):
        if hasattr(value, 'contribute_to_class'):
            value.contribute_to_class(cls, name)
        else:
            """
            All method added to Profile models was writter for User model
            So ``self`` is not of the good type.
            """
            if callable(value):
                def wrapped_method(func):
                    def wrapped(*args, **kwargs):
                        from askbot.models import get_profile_model
                        
                        new_args = []
                        for arg in args:
                            
                            if isinstance(arg, get_profile_model()):
                                new_args.append(arg.user)
                                info("%s must be relocated" % name)
                            else:
                                new_args.append(arg)
                                
                        return func(*new_args, **kwargs)
                    return wrapped
                
                value = wrapped_method(value)
                
            setattr(cls, name, value)


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