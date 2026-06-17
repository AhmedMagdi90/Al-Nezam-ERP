from django.db.models.signals import post_save
from django.contrib.auth.models import User
from django.dispatch import receiver
from django.db import DatabaseError
from .models import Profile

@receiver(post_save, sender=User)
def create_or_update_user_profile(sender, instance, created, **kwargs):
    """
    Keep profile row in sync with user changes.
    Be defensive when tenant schema is temporarily behind migrations.
    """
    try:
        db_alias = instance._state.db or 'default'
        if created:
            Profile.objects.using(db_alias).get_or_create(user_id=instance.id)
        else:
            profile = Profile.objects.using(db_alias).filter(user_id=instance.id).first()
            if profile:
                profile.save(using=db_alias)
    except (DatabaseError, ValueError):
        # Avoid breaking auth flows due to transient tenant migration drift.
        return
