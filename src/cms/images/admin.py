"""Admin settings for the image management application."""


from django import template
from django.conf.urls.defaults import patterns, url
from django.contrib import admin
from django.shortcuts import render_to_response
from django.template.defaultfilters import filesizeformat

from cms.core.admin import site
from cms.images.models import Category, Image


class CategoryAdmin(admin.ModelAdmin):
    
    """Admin settings for Category models."""
    
    list_display = ("name",)
    
    search_fields = ("name",)
    
    
site.register(Category, CategoryAdmin)


class ImageAdmin(admin.ModelAdmin):
    
    """Admin settings for File models."""
    
    list_display = ("title", "category", "width", "height", "size")
    
    list_filter = ("category",)
    
    search_fields = ("title",)
    
    # Custom actions.
    
    def get_actions(self, request):
        """Generates the actions for assigning categories."""
        actions = super(ImageAdmin, self).get_actions(request)
        # Add the dynamic file categories.
        for category in Category.objects.all():
            action_function = lambda model_admin, request, queryset: queryset.update(category=category)
            action_description = "Move selected images to %s" % category.name
            action_name = action_description.lower().replace(" ", "_")
            actions[action_name] = (action_function, action_name, action_description)
        # Add the remove category action.
        remove_category_function = self.__class__.remove_category
        remove_category_description = "Remove selected images from category"
        remove_category_name = "remove_category"
        actions[remove_category_name] = (remove_category_function, remove_category_name, remove_category_description)
        return actions
    
    def remove_category(self, request, queryset):
        """Removes the category from selected images."""
        queryset.update(category=None)
    
    # Custom display routines.
    
    def size(self, obj):
        """Returns the size of the file in a human-readable format."""
        return filesizeformat(obj.image.size)
    
    # Custom views.
    
    def get_urls(self):
        """Adds some custom functionality to the file admin."""
        admin_view_wrapper = self.admin_site.admin_view
        urlpatterns = patterns("",
                               url(r"^tiny-mce-image-list.js$", admin_view_wrapper(self.tiny_mce_image_list), name="admin_images_image_tiny_mce_image_list"),)
        urlpatterns += super(ImageAdmin, self).get_urls()
        return urlpatterns
    
    def tiny_mce_image_list(self, request):
        """Renders the TinyMCE image list."""
        images = self.queryset(request)
        context = {"images": images}
        return render_to_response("admin/images/image/tiny_mce_image_list.js", context, template.RequestContext(request), mimetype="text/javascript")
    

site.register(Image, ImageAdmin)

