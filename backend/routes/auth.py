import os
import random
import smtplib
from datetime import datetime, timedelta
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from flask import Blueprint, request, jsonify

from core.db import get_ist_now, ensure_ist
from core.auth import validate_srm_email, ADMIN_EMAIL_MAPPINGS, VALID_ADMINS, is_otp_enabled, ALLOWED_DOMAIN, check_admin_password
from core.limiter import limiter
from core.session import issue_token
from services.security_service import resolve_role
from middleware.db_middleware import (
    fetch_user_by_email, check_user_has_feedback, fetch_pending_otp,
    save_pending_otp, mark_user_verified
)

auth_bp = Blueprint('auth', __name__)


def _as_dt(value):
    """Postgres returns timestamp columns as datetime, older rows stored ISO strings: accept both."""
    return ensure_ist(value if isinstance(value, datetime) else datetime.fromisoformat(str(value)))


def _needs_otp(email, is_admin=False):
    """Students may sign in directly when OTP is disabled (local demos). Staff can unlock staff-only documents, so
    a staff address always has to prove it owns the inbox (the OTP is printed to the server console when SMTP isn't
    configured). Admin console logins have already checked the admin password."""
    if is_otp_enabled():
        return True
    return not is_admin and resolve_role(email) != 'student'

@auth_bp.route('/auth/check', methods=['POST'])
def check_email():
    """Checks returning user's feedback status without OTP using middleware"""
    data = request.json
    email = data.get('email')
    
    if not email or not validate_srm_email(email):
        return jsonify({'error': 'Invalid email'}), 400
        
    try:
        has_feedback = check_user_has_feedback(email)
        return jsonify({
            'valid': True, 
            'email': email,
            'hasSubmittedFeedback': has_feedback
        })
    except Exception as e:
        return jsonify({'error': 'Database error'}), 500

@auth_bp.route('/auth/request-otp', methods=['POST'])
@limiter.limit("5 per minute", error_message="Too many OTP requests. Please wait a minute before requesting another OTP.")
def request_otp():
    data = request.json
    email = data.get('email')
    password = data.get('password')
    
    is_admin = False
    admin_username = None
    
    if email in ADMIN_EMAIL_MAPPINGS:
        admin_username = email
        if not check_admin_password(admin_username, password):
            return jsonify({'error': 'Invalid admin password'}), 401
            
        email = ADMIN_EMAIL_MAPPINGS[admin_username]
        is_admin = True
    
    if not is_admin and (not email or not validate_srm_email(email)):
        return jsonify({'error': 'Error: Invalid username or password. Please try again.'}), 400

    if not _needs_otp(email, is_admin):
        try:
            mark_user_verified(email)
            has_feedback = check_user_has_feedback(email)
            print(f"🔓 [AUTH]: Direct login for {email} (OTP authentication disabled)")
            return jsonify({
                'success': True, 
                'bypass': True, 
                'hasSubmittedFeedback': has_feedback,
                'token': issue_token(email),
                'message': 'OTP disabled. Direct login successful.'
            })
        except Exception as e:
            print(f"⚠️ Direct login failed: {e}")

    try:
        user = fetch_user_by_email(email)
        # "signed in within 2 hours" skips the OTP for students only: the check is keyed by email alone, so it must
        # never hand out a privileged (staff) session
        if user and user['verified'] == 1 and user['last_login'] and not is_admin and resolve_role(email) == 'student':
            last_login = _as_dt(user['last_login'])
            if get_ist_now() < (last_login + timedelta(hours=2)):
                has_feedback = check_user_has_feedback(email)
                print(f"🔓 [AUTH]: Bypassing OTP for {email} (Recent login within 2 hours)")
                return jsonify({
                    'success': True, 
                    'bypass': True, 
                    'hasSubmittedFeedback': has_feedback,
                    'token': issue_token(email),
                    'message': 'Welcome back! You are still within your 2-hour session window.'
                })
    except Exception as e:
        print(f"⚠️ Bypass check failed: {e}")
        
    otp = str(random.randint(100000, 999999))
    expiry = (get_ist_now() + timedelta(hours=2)).isoformat()
    
    def send_otp_email(recipient, otp_code):
        sender_email = os.environ.get("SMTP_EMAIL", "")
        sender_password = os.environ.get("SMTP_PASSWORD", "")
        if not sender_email or not sender_password:
            # local setups without SMTP: the operator reads the code from the API console
            print(f"🔐 SMTP not configured -> OTP for {recipient}: {otp_code}")
            return
            
        try:
            msg = MIMEMultipart()
            msg['From'] = f"NormBridge AI <{sender_email}>"
            msg['To'] = recipient
            msg['Subject'] = "NormBridge AI - Your Login OTP"
            
            body = f"Hello,\n\nYour One-Time Password (OTP) for logging into NormBridge AI is: {otp_code}\n\nThis OTP is valid for 2 hours.\n\nThank you,\nNormBridge AI Team"
            msg.attach(MIMEText(body, 'plain'))
            
            server = smtplib.SMTP('smtp.gmail.com', 587, timeout=10)
            server.starttls()
            server.login(sender_email, sender_password)
            server.send_message(msg)
            server.quit()
            print(f"📧 Email sent successfully to {recipient}")
        except Exception as e:
            print(f"❌ Failed to send email: {e}")
            print(f"🔐 Fallback OTP display -> To: {recipient}, OTP: {otp_code}")

    send_otp_email(email, otp)
    
    try:
        save_pending_otp(email, otp, expiry)
        return jsonify({'success': True, 'message': 'OTP sent successfully'})
    except Exception as e:
        print(f"OTP error: {e}")
        return jsonify({'error': 'Database error'}), 500

@auth_bp.route('/auth/validate', methods=['POST'])
def validate_email():
    data = request.json
    email = data.get('email')
    otp_input = data.get('otp')
    
    if not email or not validate_srm_email(email):
        return jsonify({'error': f'Invalid email domain. Please use @{ALLOWED_DOMAIN}'}), 400
    
    if not _needs_otp(email):
        mark_user_verified(email)
        has_feedback = check_user_has_feedback(email)
        return jsonify({
            'valid': True, 
            'email': email,
            'hasSubmittedFeedback': has_feedback,
            'token': issue_token(email)
        })

    if not otp_input:
        return jsonify({'error': 'OTP is required'}), 400
        
    try:
        user = fetch_pending_otp(email)
        
        if not user or not user['otp']:
            return jsonify({'error': 'Please request an OTP first'}), 400
            
        expiry = _as_dt(user['otp_expiry'])
        if get_ist_now() > expiry:
            return jsonify({'error': 'OTP has expired. Please request a new one.'}), 400
            
        if user['otp'] != otp_input:
            return jsonify({'error': 'Invalid OTP. Please try again.'}), 400
            
        mark_user_verified(email)
        has_feedback = check_user_has_feedback(email)
        
        return jsonify({
            'valid': True, 
            'email': email,
            'hasSubmittedFeedback': has_feedback,
            'token': issue_token(email)
        })
    except Exception as e:
        print(f"Auth error: {e}")
        return jsonify({'error': 'Database error'}), 500
