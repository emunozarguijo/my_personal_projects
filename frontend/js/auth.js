// Cuenta: inicio de sesión, registro, cierre de sesión y cambio de contraseña. Arranca la app (boot).
// La sesión vive en una cookie HttpOnly que maneja el navegador: aquí no se guarda ningún token.

const auth={mode:'login',inviteRequired:false};

async function authCall(path,body){
  const r=await fetch(path,{method:'POST',headers:{'Content-Type':'application/json'},body:body?JSON.stringify(body):undefined});
  let data=null;try{data=await r.json();}catch(e){}
  if(!r.ok){
    const d=data&&data.detail;
    throw new Error(typeof d==='string'?d:Array.isArray(d)?'Revisa los datos del formulario.':'No se pudo completar ('+r.status+').');
  }
  return data;
}

function showError(el,msg){el.textContent=msg||'';el.hidden=!msg;}

function setAuthMode(mode){
  auth.mode=mode;
  document.querySelectorAll('.auth-tabs .tab').forEach(t=>t.setAttribute('aria-selected',String(t.dataset.mode===mode)));
  document.querySelectorAll('#auth-form [data-only="register"]').forEach(el=>{el.hidden=mode!=='register';});
  $('#auth-invite-wrap').hidden=mode!=='register'||!auth.inviteRequired;
  $('#auth-pass').autocomplete=mode==='register'?'new-password':'current-password';
  $('#auth-submit').textContent=mode==='register'?'Crear cuenta':'Entrar';
  showError($('#auth-error'),'');
}

function showAuth(message){
  $('#shell').hidden=true;$('#auth').hidden=false;
  setAuthMode(auth.mode);
  if(message)showError($('#auth-error'),message);
  $('#auth-user').focus();
}

function enterApp(user){
  state.user=user;
  $('#account-name').textContent=user.username+(user.role==='admin'?' · admin':'');
  $('#auth').hidden=true;$('#shell').hidden=false;
  loadCategories().then(()=>loadStatements());
}

// Llamada desde api() (app.js) cuando el servidor responde 401 a mitad del uso.
function onSessionExpired(){
  if(!$('#auth').hidden)return;
  state.user=null;state.statements=[];state.selected=null;state.detail=null;state.chats={};
  showAuth('Tu sesión terminó. Vuelve a entrar.');
}

document.querySelectorAll('.auth-tabs .tab').forEach(t=>t.addEventListener('click',()=>setAuthMode(t.dataset.mode)));

$('#auth-form').addEventListener('submit',async e=>{
  e.preventDefault();
  const username=$('#auth-user').value.trim(),password=$('#auth-pass').value,err=$('#auth-error');
  if(!username||!password)return showError(err,'Escribe tu usuario y tu contraseña.');
  const body={username,password};
  if(auth.mode==='register'){
    if(password.length<10)return showError(err,'La contraseña debe tener al menos 10 caracteres.');
    if(password!==$('#auth-pass2').value)return showError(err,'Las contraseñas no coinciden.');
    if(auth.inviteRequired)body.invite_code=$('#auth-invite').value.trim();
  }
  const btn=$('#auth-submit');btn.disabled=true;
  try{
    const user=await authCall(auth.mode==='register'?'/auth/register':'/auth/login',body);
    $('#auth-form').reset();
    enterApp(user);
    if(auth.mode==='register')toast('¡Listo, '+user.username+'! Carga tu primer estado de cuenta.','ok');
  }catch(ex){showError(err,ex.message);}
  finally{btn.disabled=false;}
});

$('#logout').addEventListener('click',async()=>{
  try{await authCall('/auth/logout');}catch(e){}
  location.reload();  // limpia por completo el estado en memoria de la cuenta anterior
});

/* ---------- Cambiar contraseña ---------- */
const pwDialog=$('#pw-dialog');
$('#pw-open').addEventListener('click',()=>{$('#pw-form').reset();showError($('#pw-error'),'');pwDialog.showModal();$('#pw-current').focus();});
$('#pw-cancel').addEventListener('click',()=>pwDialog.close());
$('#pw-form').addEventListener('submit',async e=>{
  e.preventDefault();
  const err=$('#pw-error'),current=$('#pw-current').value,next=$('#pw-new').value;
  if(next.length<10)return showError(err,'La contraseña nueva debe tener al menos 10 caracteres.');
  if(next!==$('#pw-new2').value)return showError(err,'Las contraseñas nuevas no coinciden.');
  try{
    await authCall('/auth/password',{current_password:current,new_password:next});
    pwDialog.close();toast('Contraseña actualizada.','ok');
  }catch(ex){showError(err,ex.message);}
});

/* ---------- Arranque ---------- */
async function boot(){
  try{auth.inviteRequired=(await (await fetch('/auth/config')).json()).invite_required;}catch(e){}
  const r=await fetch('/auth/me').catch(()=>null);
  if(r&&r.ok)enterApp(await r.json());
  else showAuth();
}
boot();
