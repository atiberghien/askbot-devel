from django import template

register = template.Library()

class IncludeJinja(template.Node):
    """http://www.mellowmorning.com/2010/08/24/"""
    def __init__(self, filename, request_var):
        self.filename = filename
        self.request_var = template.Variable(request_var)
    def render(self, context):
        from askbot.skins.loaders import get_template
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

