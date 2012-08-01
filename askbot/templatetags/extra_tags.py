import math
from django import template
from askbot.skins.loaders import get_template

register = template.Library()
    
@register.simple_tag
def get_tag_font_size(tags):
    max_tag = 0
    for tag in tags:
        if tag.used_count > max_tag:
            max_tag = tag.used_count

    min_tag = max_tag
    for tag in tags:
        if tag.used_count < min_tag:
            min_tag = tag.used_count

    font_size = {}
    for tag in tags:
        font_size[tag.name] = tag_font_size(max_tag,min_tag,tag.used_count)
    
    return font_size

@register.simple_tag
def tag_font_size(max_size, min_size, current_size):
    """
    do a logarithmic mapping calcuation for a proper size for tagging cloud
    Algorithm from http://blogs.dekoh.com/dev/2007/10/29/choosing-a-good-
    font-size-variation-algorithm-for-your-tag-cloud/
    """
    MAX_FONTSIZE = 10
    MIN_FONTSIZE = 1

    #avoid invalid calculation
    if current_size == 0:
        current_size = 1    
    try:
        weight = (math.log10(current_size) - math.log10(min_size)) / (math.log10(max_size) - math.log10(min_size))
    except Exception:
        weight = 0
        
    return int(MIN_FONTSIZE + round((MAX_FONTSIZE - MIN_FONTSIZE) * weight))


class IncludeJinja(template.Node):
    """http://www.mellowmorning.com/2010/08/24/"""
    def __init__(self, filename, request_var):
        self.filename = filename
        self.request_var = template.Variable(request_var)
    def render(self, context):
        request = self.request_var.resolve(context)
        jinja_template = get_template(self.filename, request)
        return jinja_template.render(context)

@register.tag
def include_jinja(parser, token):
    bits = token.contents.split()

    #Check if a filename was given
    if len(bits) != 3:
        error_message = '%r tag requires the name of the template and the request variable'
        raise template.TemplateSyntaxError(error_message % bits[0])
    filename = bits[1]
    request_var = bits[2]

    #Remove quotes or raise error
    if filename[0] in ('"', "'") and filename[-1] == filename[0]:
        filename = filename[1:-1]
    else:
        raise template.TemplateSyntaxError('file name must be quoted')

    
    return IncludeJinja(filename, request_var)

