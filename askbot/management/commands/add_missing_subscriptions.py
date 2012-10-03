from django.core.management.base import NoArgsCommand
from django.db import transaction
from userena.utils import get_profile_model

class Command(NoArgsCommand):
    @transaction.commit_manually
    def handle_noargs(self, **options):
        for profile in get_profile_model().objects.all():
            profile.add_missing_askbot_subscriptions()
            transaction.commit()
