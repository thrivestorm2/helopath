

# The aircraft admin form carries the service-history inline; posts need its (empty) management form.
PERIODS_INLINE = {"past_periods-TOTAL_FORMS": "0", "past_periods-INITIAL_FORMS": "0",
                  "past_periods-MIN_NUM_FORMS": "0", "past_periods-MAX_NUM_FORMS": "1000"}


def login_viewer(client, username="viewer"):
    """Log `client` in as a Viewer-group user (who only ever sees confirmed aircraft)."""
    from django.contrib.auth.models import Group, User
    user = User.objects.create_user(username, password="x")
    user.groups.add(Group.objects.get(name="Viewer"))
    client.force_login(user)
    return user


def test_operator(name="Test Operator"):
    """Confirmed aircraft need an operator; fixtures that do not care which use this one."""
    from tracker.models import Operator
    return Operator.objects.get_or_create(name=name)[0]
