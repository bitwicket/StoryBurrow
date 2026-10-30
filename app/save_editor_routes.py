import hmac
import tempfile
from pathlib import Path
from flask import Blueprint,abort,render_template,request,session,redirect,url_for,flash
from werkzeug.exceptions import HTTPException
from save_actions_routes import guarded_root
from save_operations import LOCK,safe_file,_file,replace
from inspect_variables import inspect,InspectionError
from save_variable_editor import make_edit,parse_value,EditError

bp=Blueprint('save_editor',__name__)

@bp.errorhandler(HTTPException)
def errors(e):
    return render_template('message.html',title='Save editor unavailable',message=e.description),e.code

def get_file(game_id,filename):
    root=guarded_root(game_id)
    try: name=safe_file(filename)
    except ValueError:abort(404)
    if not name.endswith('.save'):abort(404)
    path=root/'current'/name
    if not _file(path):abort(404)
    return root,name,path

@bp.get('/games/<game_id>/saves/editor/<filename>')
def editor(game_id,filename):
    with LOCK:
        root,name,path=get_file(game_id,filename)
        try:data=inspect(path)
        except (InspectionError,OSError,ValueError,KeyError) as e:
            abort(422,description=str(e))
    from main import csrf_token
    variables=[{'name':k,**v} for k,v in sorted(data['primitive_variables'].items())]
    return render_template('save_editor.html',game_id=game_id,filename=name,variables=variables,unsupported=data['unsupported_variables'],csrf=csrf_token(),saved=request.args.get('saved')=='1')

@bp.post('/games/<game_id>/saves/editor/<filename>')
def save_edit(game_id,filename):
    if not hmac.compare_digest(request.form.get('csrf_token',''),session.get('_csrf','not-set')):abort(400,description='Invalid form token')
    with LOCK:
        root,name,path=get_file(game_id,filename)
        variable=request.form.get('variable','')
        if len(variable)>200:abort(400)
        try:
            data=inspect(path)['primitive_variables']
            if variable not in data:raise EditError('Variable not editable')
            kind=data[variable]['type']
            previous=parse_value(kind,request.form.get('expected',''))
            new=parse_value(kind,request.form.get('value',''))
            if previous==new:return redirect(url_for('.editor',game_id=game_id,filename=name),code=303)
            with tempfile.TemporaryDirectory(prefix='storyburrow-variable-edit-') as td:
                target=Path(td)/'edited.save'
                make_edit(path,variable,previous,new,target)
                # Second status check before write; global manager lock is still a known concurrency limitation.
                guarded_root(game_id)
                replace(root,name,target,'edit')
        except (EditError,InspectionError,ValueError,OSError,KeyError) as e:
            abort(409,description=f'Edit rejected: {e}')
    return redirect(url_for('.editor',game_id=game_id,filename=name,saved='1'),code=303)
