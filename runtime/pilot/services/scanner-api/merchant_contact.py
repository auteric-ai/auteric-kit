"""Deliver merchant connection requests to the Auteric inbox."""

from __future__ import annotations

import os
import re
import smtplib
from email.message import EmailMessage

from fastapi import HTTPException
from pydantic import BaseModel, Field

from public_reports import domain_name


class ConnectionRequest(BaseModel):
    domain: str = Field(max_length=253)
    email: str = Field(max_length=254)
    phone: str = Field(max_length=32)
    website: str = Field(default='', max_length=100)  # Hidden spam trap.


def send_connection_request(request: ConnectionRequest) -> None:
    host = os.getenv('AUTERIC_CONTACT_SMTP_HOST', '').strip()
    sender = os.getenv('AUTERIC_CONTACT_FROM_EMAIL', '').strip()
    if not host or not sender:
        raise HTTPException(503, 'Email delivery is not configured')
    port = int(os.getenv('AUTERIC_CONTACT_SMTP_PORT', '587'))
    user = os.getenv('AUTERIC_CONTACT_SMTP_USER', '').strip()
    password = os.getenv('AUTERIC_CONTACT_SMTP_PASSWORD', '')
    message = EmailMessage()
    message['Subject'] = f'Store connection request: {request.domain}'
    message['From'] = sender
    message['To'] = 'hello@auteric.com'
    message['Reply-To'] = request.email
    message.set_content(f'Store: {request.domain}\nEmail: {request.email}\nPhone: {request.phone}\n')
    try:
        with smtplib.SMTP(host, port, timeout=10) as smtp:
            smtp.ehlo()
            smtp.starttls()
            smtp.ehlo()
            if user:
                smtp.login(user, password)
            smtp.send_message(message)
    except (OSError, smtplib.SMTPException) as exc:
        raise HTTPException(502, 'Email delivery failed. Please use your email app instead.') from exc


def install_contact_routes(app) -> None:
    @app.post('/api/onboarding/contact')
    def connection_contact(request: ConnectionRequest):
        try:
            request.domain = domain_name(request.domain)
        except (ValueError, UnicodeError):
            raise HTTPException(422, 'Enter a valid store domain') from None
        request.email = request.email.strip().lower()
        request.phone = request.phone.strip()
        if not re.fullmatch(r'[^\s@<>]+@[^\s@<>]+\.[^\s@<>]+', request.email):
            raise HTTPException(422, 'Enter a valid email address')
        if not re.fullmatch(r'[+()0-9 .-]{7,32}', request.phone) or len(re.sub(r'\D', '', request.phone)) < 7:
            raise HTTPException(422, 'Enter a valid phone number')
        if request.website:
            return {'status': 'received'}
        send_connection_request(request)
        return {'status': 'sent'}
