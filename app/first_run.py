import hmac
import re
from flask import Blueprint, abort, redirect, render_template, request, session, url_for
from auth_state import create_first_admin

bp = Blueprint('first_run', __name__)

@bp.route('/setup', methods=['GET', 'POST'])
def setup():
    from main import has_admin, csrf_token
    if has_admin():
        return redirect(url_for('library') if session.get('user') else url_for('login'), code=303)
    error = None
    if request.method == 'POST':
        if not hmac.compare_digest(request.form.get('csrf_token', ''), session.get('_csrf', 'not-set')):
            abort(400, description='Invalid form token')
        username = request.form.get('username','').strip()
        password = request.form.get('password','')
        confirmation = request.form.get('confirm_password','')
        if not re.fullmatch(r'[A-Za-z0-9_.-]{1,40}', username):
            error = 'Username must contain 1–40 letters, numbers, underscores, dots or hyphens.'
        elif len(password) < 6 or len(password) > 1024:
            error = 'Password must contain 6–1024 characters.'
        elif password != confirmation:
            error = 'Passwords do not match.'
        else:
            try:
                rev = create_first_admin(username, password)
            except ValueError:
                return redirect(url_for('login'), code=303)
            session.clear()
            session['user'] = username
            session['auth_revision'] = rev
            session.permanent = True
            return redirect(url_for('library'), code=303)
    return render_template('first_run.html', error=error, csrf=csrf_token())