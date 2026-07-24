from django.contrib.auth.models import Group, User
from django.test import TestCase

from operations.models import StaffMember
from operations.services.authority import (
    AuthorityRole,
    authority_role,
    is_center_operator,
    is_director_user,
)


class AuthorityPolicyTests(TestCase):
    def test_superuser_is_director_and_center_operator(self):
        user = User.objects.create_superuser("director", password="x")

        self.assertEqual(authority_role(user), AuthorityRole.DIRECTOR)
        self.assertTrue(is_director_user(user))
        self.assertTrue(is_center_operator(user))

    def test_director_group_grants_director_role_without_staff_flag(self):
        group = Group.objects.create(name="Руководители")
        user = User.objects.create_user("director-group", password="x")
        user.groups.add(group)

        self.assertEqual(authority_role(user), AuthorityRole.DIRECTOR)
        self.assertTrue(is_center_operator(user))

    def test_staff_user_is_administrator(self):
        user = User.objects.create_user("administrator", password="x", is_staff=True)

        self.assertEqual(authority_role(user), AuthorityRole.ADMINISTRATOR)
        self.assertFalse(is_director_user(user))

    def test_linked_staff_user_is_specialist_without_management_access(self):
        user = User.objects.create_user("specialist", password="x")
        StaffMember.objects.create(user=user, full_name="Тестовый специалист")

        self.assertEqual(authority_role(user), AuthorityRole.SPECIALIST)
        self.assertFalse(is_center_operator(user))
