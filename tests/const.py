"""Test constants."""
HOST = "https://erp.example.com"
TOKEN = "test-jwt"
SECRET = "a" * 64

ICAL = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//inwendo//test//EN
BEGIN:VEVENT
UID:booking-1@erp
DTSTART:20300101T100000Z
DTEND:20300101T110000Z
SUMMARY:Meeting
END:VEVENT
END:VCALENDAR
"""
