from datetime import datetime
from zoneinfo import ZoneInfo

from members.models import Member

IST = ZoneInfo("Asia/Kolkata")


def ist(year, month, day, hour=9, minute=0, second=0):
    return datetime(year, month, day, hour, minute, second, tzinfo=IST)


def give_email(member, email="asha@example.com"):
    Member.objects.filter(pk=member.pk).update(email=email)
    member.refresh_from_db()
    return member
