from django.core.management.base import NoArgsCommand
import random
from django.contrib.auth.models import User
from askbot.models.tag import Tag

class Command(NoArgsCommand):
    def handle_noargs(self, **options):
        user = User.objects.all()[random.randint(0, User.objects.count())]
        for i in xrange(1000):
            name = 'tag' + str(i)
            Tag.objects.create(name = name,created_by = user)
