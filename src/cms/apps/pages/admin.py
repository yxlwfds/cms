"""
The upgraded CMS online admin area.

This is an enhanced version of the Django admin area, providing a more
user-friendly appearance and providing additional functionality over the
standard implementation.
"""

from __future__ import with_statement

import urllib, json

from django.core.exceptions import PermissionDenied
from django.core.urlresolvers import reverse
from django.conf.urls.defaults import patterns, url
from django.db import transaction
from django.http import Http404, HttpResponseRedirect, HttpResponse, HttpResponseForbidden
from django.shortcuts import render, redirect

from cms.core.admin import PageBaseAdmin, site
from cms.core.db import locked
from cms.apps.pages import content
from cms.apps.pages.models import Page


# The GET parameter used to indicate where page admin actions originated.
PAGE_FROM_KEY = "from"

# The GET parameter value used to indicate that the page admin action came form the sitemap.
PAGE_FROM_SITEMAP_VALUE = "sitemap"

# The GET parameter used to indicate content type.
PAGE_TYPE_PARAMETER = "type"
    
    
class PageAdmin(PageBaseAdmin):

    """Admin settings for Page models."""

    publication_fieldsets = (("Publication", {"fields": ("publication_date", "expiry_date", "is_online",),
                                              "classes": ("collapse",)}),)

    navigation_fieldsets = (("Navigation", {"fields": ("short_title", "permalink", "in_navigation",),
                                            "classes": ("collapse",),},),)

    fieldsets = ((None, {"fields": ("title", "url_title", "parent",),},),) + publication_fieldsets + navigation_fieldsets + PageBaseAdmin.seo_fieldsets
    
    # Reversion

    def get_revision_form_data(self, request, obj, version):
        """
        Returns a dictionary of data to set in the admin form in order to revert
        to the given revision.
        """
        data = super(PageAdmin, self).get_revision_form_data(request, obj, version)
        content_data = version.object_version.object.content.data
        data.update(content_data)
        return data

    # Plugable content types.

    def get_page_content_type(self, request, obj=None):
        """Retrieves the page content type slug."""
        if PAGE_TYPE_PARAMETER in request.GET:
            return request.GET[PAGE_TYPE_PARAMETER]
        if obj and obj.content_type:
            return obj.content_type
        raise Http404, "You must specify a page content type."

    def get_page_content(self, request, obj=None):
        """Retrieves the page content object."""
        page_content_type = self.get_page_content_type(request, obj)
        page_content_cls = content.lookup(page_content_type)
        # Create new page content instance.
        page_content = page_content_cls(obj)
        return page_content

    def get_fieldsets(self, request, obj=None):
        """Generates the custom content fieldsets."""
        page_content = self.get_page_content(request, obj)
        content_fieldsets = page_content.get_fieldsets()
        fieldsets = super(PageBaseAdmin, self).get_fieldsets(request, obj)
        fieldsets = fieldsets[0:1] + content_fieldsets + fieldsets[1:]
        return fieldsets

    def get_form(self, request, obj=None, **kwargs):
        """Adds the template area fields to the form."""
        page_content = self.get_page_content(request, obj)
        Form = page_content.get_form()
        defaults = {"form": Form}
        defaults.update(kwargs)
        PageForm = super(PageAdmin, self).get_form(request, obj, **defaults)
        # HACK: Need to limit parents field based on object. This should be done in
        # formfield_for_foreignkey, but that method does not know about the object instance.
        if obj:
            invalid_parents = frozenset(obj.all_children + [obj])
        else:
            invalid_parents = frozenset()
        try:
            homepage = Page.objects.get_homepage()
        except Page.DoesNotExist:
            parent_choices = []
        else:
            parent_choices = []
            for page in [homepage] + homepage.all_children:
                if not page in invalid_parents:
                    parent_choices.append((page.id, u" \u203a ".join(unicode(breadcrumb) for breadcrumb in page.breadcrumbs)))
        if not parent_choices:
            parent_choices = (("", "---------"),)
        PageForm.base_fields["parent"].choices = parent_choices
        # Return the completed form.
        return PageForm

    def save_model(self, request, obj, form, change):
        """Saves the model and adds its content fields."""
        # Create the page content.
        page_content_type = self.get_page_content_type(request, obj)
        page_content = self.get_page_content(request, obj)
        for field_name in page_content.get_field_names():
            field_data = form.cleaned_data[field_name]
            setattr(page_content, field_name, field_data)
        obj.content_type = page_content_type
        obj.content = page_content
        # Get the page order.
        if not obj.order:
            with locked(Page):
                try:
                    obj.order = self.model.objects.order_by("-order").values_list("order", flat=True)[0] + 1
                except IndexError:
                    obj.order = 1
        # Save the model.
        super(PageBaseAdmin, self).save_model(request, obj, form, change)

    # Custom views.

    def get_urls(self):
        """Generates custom admin URLS."""
        urls = super(PageAdmin, self).get_urls()
        admin_view = self.admin_site.admin_view
        urls = patterns("",
            url(r"^move-page/$", admin_view(self.move_page), name="pages_page_move_page"),
            url(r"^sitemap.json$", admin_view(self.sitemap_json), name="pages_page_sitemap_json"),
        ) + urls
        return urls

    @transaction.commit_on_success
    def move_page(self, request):
        """Moves a page up or down."""
        page = Page.objects.get_by_id(request.POST["page"])
        # Check that the user has permission to move the page.
        if not self.has_move_permission(request, page):
            return HttpResponseForbidden("You do not have permission to move this page.")
        # Get the page to swap with.
        direction = request.POST["direction"]
        parent = page.parent
        if parent is not None:
            try:
                if direction == "up":
                    other = parent.children.order_by("-order").filter(order__lt=page.order)[0]
                elif direction == "down":
                    other = parent.children.order_by("order").filter(order__gt=page.order)[0]
                else:
                    raise ValueError, "Direction should be 'up' or 'down', not '%s'." % direction
            except IndexError:
                # Impossible to move pag up or down because it already is at the top or bottom!
                pass
            else:
                with locked(Page):
                    page_order = page.order
                    other_order = other.order
                    page.order = other_order
                    other.order = page_order
                    page.save()
                    other.save()
        # Return a response appropriate to whether this was an AJAX request or not.
        if request.is_ajax():
            return HttpResponse("Page #%s was moved %s." % (page.id, direction))
        else:
            return redirect("admin:index")
    
    def sitemap_json(self, request):
        """Returns a JSON data structure describing the sitemap."""
        # Get the homepage.
        try:
            homepage = Page.objects.get_homepage()
        except Page.DoesNotExist:
            homepage = None
        # Compile the initial data.
        data = {
            "canAdd": self.has_add_permission(request),
            "canChange": self.has_change_permission(request),
            "createHomepageUrl": reverse("admin:pages_page_add") + "?{0}={1}".format(PAGE_FROM_KEY, PAGE_FROM_SITEMAP_VALUE)
        }
        # Add in the page data.
        if homepage:
            def sitemap_entry(page):
                children = []
                for child in page.children:
                    children.append(sitemap_entry(child))
                return {
                    "hasParent": page.parent is not None,
                    "isOnline": page.is_online,
                    "id": page.id,
                    "title": unicode(page),
                    "canChange": self.has_change_permission(request, page),
                    "canDelete": self.has_delete_permission(request, page),
                    "canMove": self.has_move_permission(request, page),
                    "addUrl": reverse("admin:pages_page_add") + "?%s=%s&parent=%i" % (PAGE_FROM_KEY, PAGE_FROM_SITEMAP_VALUE, page.id),
                    "changeUrl": reverse("admin:pages_page_change", args=(page.pk,)) + "?%s=%s" % (PAGE_FROM_KEY, PAGE_FROM_SITEMAP_VALUE),
                    "deleteUrl": reverse("admin:pages_page_delete", args=(page.pk,)) + "?%s=%s" % (PAGE_FROM_KEY, PAGE_FROM_SITEMAP_VALUE),
                    "children": children,
                }
            data["entries"] = [sitemap_entry(homepage)]
        else:
            data["entries"] = []
        # Render the JSON.
        response = HttpResponse(content_type="application/json; charset=utf-8")
        json.dump(data, response)
        return response
    
    def patch_response_location(self, request, response):
        """Perpetuates the 'from' key in all redirect responses."""
        if isinstance(response, HttpResponseRedirect):
            if PAGE_FROM_KEY in request.GET:
                response["Location"] += "?%s=%s" % (PAGE_FROM_KEY, request.GET[PAGE_FROM_KEY])
        return response
            
    def changelist_view(self, request, *args, **kwargs):
        """Redirects to the sitemap, if appropriate."""
        if PAGE_FROM_KEY in request.GET:
            redirect_slug = request.GET[PAGE_FROM_KEY]
            if redirect_slug == PAGE_FROM_SITEMAP_VALUE:
                return redirect("admin:index")
        return super(PageAdmin, self).changelist_view(request, *args, **kwargs)
    
    def has_add_content_permission(self, request, slug):
        """Checks whether the given user can edit the given content slug."""
        model = self.model
        opts = model._meta
        # The default page add permission implicitly allows editing of the default content type.
        if slug == content.DefaultContent.registration_key:
            return True
        # Check user has correct permission.
        add_permission = "%s.%s" % (opts.app_label, content.get_add_permission(slug))
        return request.user.has_perm(add_permission)
    
    def has_move_permission(self, request, obj):
        """Checks whether the given user can move the given page."""
        return self.has_change_permission(request, obj.parent)
    
    def add_view(self, request, *args, **kwargs):
        """Ensures that a valid content type is chosen."""
        if not PAGE_TYPE_PARAMETER in request.GET:
            # Generate the available content items.
            content_items = content.registered_content.items()
            content_items.sort(lambda a, b: cmp(a[1].classifier, b[1].classifier) or cmp(a[1].verbose_name.lower(), b[1].verbose_name.lower()))
            content_types = []
            for slug, content_type in content_items:
                if self.has_add_content_permission(request, slug):
                    # If we get this far, then we have permisison to add a page of this type.
                    get_params = request.GET.items()
                    get_params.append((PAGE_TYPE_PARAMETER, slug))
                    query_string = urllib.urlencode(get_params)
                    url = request.path + "?" + query_string
                    content_type_context = {"name": content_type.verbose_name,
                                            "icon": content_type.icon,
                                            "url": url,
                                            "classifier": content_type.classifier}
                    content_types.append(content_type_context)
            # Shortcut for when there is a single content type.
            if len(content_types) == 1:
                return redirect(content_types[0]["url"])
            # Render the select page template.
            context = {"title": "Select page type",
                       "content_types": content_types}
            return render(request, "admin/pages/page/select_page_type.html", context)
        else:
            if not self.has_add_content_permission(request, request.GET[PAGE_TYPE_PARAMETER]):
                raise PermissionDenied, "You are not allowed to add pages of that content type."
        return super(PageBaseAdmin, self).add_view(request, *args, **kwargs)
    
    def response_add(self, request, *args, **kwargs):
        """Redirects to the sitemap if appropriate."""
        response = super(PageAdmin, self).response_add(request, *args, **kwargs)
        return self.patch_response_location(request, response)
    
    def response_change(self, request, *args, **kwargs):
        """Redirects to the sitemap if appropriate."""
        response = super(PageAdmin, self).response_change(request, *args, **kwargs)
        return self.patch_response_location(request, response)
    
    def delete_view(self, request, *args, **kwargs):
        """Redirects to the sitemap if appropriate."""
        response = super(PageAdmin, self).delete_view(request, *args, **kwargs)
        return self.patch_response_location(request, response)


site.register(Page, PageAdmin)
site.register_link_list(Page)