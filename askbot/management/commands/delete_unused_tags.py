from django.core.management.base import NoArgsCommand
from askbot import models
import sys

class Command(NoArgsCommand):
    def handle_noargs(self, **options):
        tags = models.Tag.objects.all()
        print "Searching for unused tags:",
        deleted_tags = list()
        for tag in tags:
            if not tag.threads.exists():
                deleted_tags.append(tag.name)
                tag.delete()

        if deleted_tags:
            found_count = len(deleted_tags)
            if found_count == 1:
                print "Found an unused tag %s" % deleted_tags[0]
            else:
                sys.stdout.write("Found %d unused tags" % found_count)
                if found_count > 50:
                    print ", first 50 are:",
                    print ', '.join(deleted_tags[:50]) + '.'
                else:
                    print ": " + ', '.join(deleted_tags) + '.'
            print "Deleted."
        else:
            print "Did not find any."

