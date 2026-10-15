// Lógica de la interfaz: estado, llamadas a la API y renderizado de vistas.
const $=s=>document.querySelector(s);
const state={statements:[],selected:null,detail:null,tab:'salud',filter:'all',cat:'',q:'',chats:{},busy:false,categories:[]};

async function api(path,opts){
  const r=await fetch(path,opts);
  let data=null;try{data=await r.json();}catch(e){}
  if(r.status===401&&!path.startsWith('/auth/')){onSessionExpired();throw new Error('Tu sesión terminó. Vuelve a entrar.');}
  if(!r.ok){
    const d=data&&data.detail;
    throw new Error(typeof d==='string'?d:Array.isArray(d)?d.map(x=>x.msg).join('; '):'No se pudo completar la acción ('+r.status+').');
  }
  return data;
}
function toast(msg,kind){
  const t=document.createElement('div');t.className='toast '+(kind||'');t.textContent=msg;
  $('#toasts').appendChild(t);setTimeout(()=>t.remove(),kind==='bad'?9000:5000);
}

/* ---------- Meses ---------- */
async function loadStatements(selectId){
  try{state.statements=await api('/statements');}
  catch(e){toast('No se pudo leer tu historial: '+e.message,'bad');return;}
  const ids=state.statements.map(s=>s.id);
  const target=ids.includes(selectId)?selectId:ids.includes(state.selected)?state.selected:ids[0]||null;
  $('#shell').classList.toggle('is-empty',!ids.length);
  renderRail(target);
  if(target)await select(target,true);else state.selected=null;
}
function renderRail(active){
  const box=$('#months');
  if(!state.statements.length){box.innerHTML='<p class="empty-note">Aún no hay meses. Carga tu primer PDF para empezar.</p>';return;}
  const years=new Map();
  for(const s of state.statements){const y=s.period_end.slice(0,4);if(!years.has(y))years.set(y,[]);years.get(y).push(s);}
  let html='';
  for(const [y,list] of years){
    html+='<div class="year">'+y+'</div><ul class="timeline">';
    for(const s of list){
      html+='<li><button class="month" type="button" data-id="'+s.id+'" aria-current="'+(s.id===active)+'">'
        +'<span class="amt">'+money(s.pay_no_interest)+'</span>'
        +'<span class="name">'+esc(monthName(s.period_end))+'</span>'
        +'<span class="sub">'+fmtDate(s.period_start,false)+' al '+fmtDate(s.period_end,false)+', tarjeta ••'+esc(s.card_last4||'----')+'</span>'
        +'</button></li>';
    }
    html+='</ul>';
  }
  box.innerHTML=html;
}
$('#months').addEventListener('click',e=>{const b=e.target.closest('.month');if(b)select(Number(b.dataset.id));});

async function select(id,keepTab){
  state.selected=id;
  document.querySelectorAll('.month').forEach(b=>b.setAttribute('aria-current',String(Number(b.dataset.id)===id)));
  try{state.detail=await api('/statements/'+id);}
  catch(e){toast(e.message,'bad');return;}
  state.filter='all';state.cat='';state.q='';
  renderHeader();renderResumen();renderTx();renderChat();
  if(state.tab==='salud')loadHealth();
  if(!keepTab)setTab(state.tab);
}
function renderHeader(){
  const d=state.detail;
  $('#title').textContent=monthTitle(d.period_end);
  $('#meta').innerHTML='<span>Tarjeta ••'+esc(d.card_last4||'----')+'</span>'
    +'<span>Del '+fmtDate(d.period_start)+' al '+fmtDate(d.period_end)+'</span>'
    +(d.days_in_period?'<span>'+d.days_in_period+' días</span>':'')
    +'<span>'+esc(d.filename)+'</span>';
}

/* ---------- Resumen ---------- */
function ledger(rows,total){
  let h='';
  for(const r of rows)h+='<div class="row"><span class="k">'+r[0]+'</span><span class="dots"></span><span class="v '+(r[2]||'')+'">'+r[1]+'</span></div>';
  if(total)h+='<div class="row total"><span class="k">'+total[0]+'</span><span class="dots"></span><span class="v">'+total[1]+'</span></div>';
  return h;
}
function renderResumen(){
  const d=state.detail;
  const due=d.due_date||d.cut_date;
  const cyc=cycleModel(d.period_start,d.cut_date,due,todayISO());
  let statusText,statusCls='';
  if(cyc.state==='paying'){statusText=cyc.daysLeft===0?'Hoy vence tu pago':'Te quedan '+cyc.daysLeft+' días para pagar';if(cyc.daysLeft<=5)statusCls='soon';}
  else if(cyc.state==='buying')statusText='Este periodo sigue abierto';
  else statusText='El plazo para pagar este mes ya terminó';

  const limit=num(d.credit_limit),avail=num(d.available_credit);
  const used=limit&&avail!==null?Math.max(0,Math.min(100,(limit-avail)/limit*100)):null;

  const purchases=d.transactions.filter(t=>t.kind==='purchase');
  const groups=new Map();
  for(const t of purchases){const k=t.description.trim();const g=groups.get(k)||{n:0,total:0};g.n++;g.total+=Number(t.amount);groups.set(k,g);}
  const top=[...groups.entries()].sort((a,b)=>b[1].total-a[1].total).slice(0,8);
  const maxv=top.length?top[0][1].total:1;
  const spent=purchases.reduce((s,t)=>s+Number(t.amount),0);

  let h='<div class="hero"><div>'
    +'<div class="hero-label">Para no pagar intereses, abona antes del '+fmtDate(d.due_date)+'</div>'
    +'<div class="hero-amount">'+money(d.pay_no_interest)+'</div>'
    +'<p class="hero-note">Con el pago mínimo, '+money(d.min_payment)+', evitas recargos por pago tardío, pero se generan intereses sobre lo que no pagues.</p>'
    +'<span class="status '+statusCls+'">'+statusText+'</span></div>'
    +'<div class="cycle"><div class="track" aria-hidden="true">'
    +'<span class="seg buy" style="width:'+cyc.buyPct.toFixed(2)+'%"></span><span class="seg pay" style="width:'+cyc.payPct.toFixed(2)+'%"></span>'
    +((cyc.state==='buying'||cyc.state==='paying')?'<span class="today" style="left:'+cyc.todayPct.toFixed(2)+'%"></span>':'')
    +'</div><div class="ticks">'
    +'<span style="left:0"><b>'+fmtDate(d.period_start,false)+'</b>Inicio</span>'
    +'<span style="left:'+cyc.buyPct.toFixed(2)+'%;transform:translateX(-50%);text-align:center"><b>'+fmtDate(d.cut_date,false)+'</b>Corte</span>'
    +'<span style="right:0;text-align:right"><b>'+fmtDate(due,false)+'</b>Fecha límite</span></div>'
    +'<div class="legend"><span><i style="background:var(--jacaranda)"></i>Compras del periodo</span><span><i style="background:var(--marigold)"></i>Plazo para pagar</span></div>'
    +'</div></div>';

  h+='<div class="grid2"><div class="panel"><h2>Cómo se calculó tu pago</h2><p class="lead">Del periodo anterior a lo que debes hoy.</p>'
    +ledger([
      ['Adeudo del periodo anterior',money(d.previous_balance)],
      ['Cargos regulares',money(d.regular_charges)],
      ['Compras y cargos a meses (capital)',money(d.installment_capital)],
      ['Intereses',money(d.interest)],
      ['Comisiones',money(d.commissions)],
      ['IVA de intereses y comisiones',money(d.iva)],
      ['Pagos y abonos','−'+money(d.payments),'pos']
    ],['Pago para no generar intereses',money(d.pay_no_interest)])
    +'</div><div class="panel"><h2>Tu tarjeta</h2><p class="lead">Crédito y costo del financiamiento.</p>'
    +(used!==null?'<div class="meter" role="img" aria-label="Has usado '+used.toFixed(0)+'% de tu límite"><i style="width:'+used.toFixed(1)+'%"></i></div><div class="meter-cap"><span>Usado '+used.toFixed(0)+'%</span><span>Límite '+money(d.credit_limit)+'</span></div>':'')
    +ledger([
      ['Crédito disponible',money(d.available_credit)],
      ['Pago mínimo',money(d.min_payment)],
      ['Mínimo más mensualidades a meses',money(d.min_plus_installments)],
      ['CAT, sin IVA',pct(d.cat)],
      ['Tasa de interés anual',pct(d.annual_rate)],
      ['Intereses pagados en 12 meses',money(d.interest_last_12m)]
    ])+'</div></div>';

  if(top.length){
    h+='<div class="block"><h2>En qué se fue el dinero</h2><p class="lead">'+purchases.length+' compras por '+money(spent)+' este periodo.</p>';
    for(const [name,g] of top){
      h+='<div class="bar"><span class="nm">'+esc(name)+(g.n>1?' <small>('+g.n+' compras)</small>':'')+'</span>'
        +'<span class="tr"><i style="width:'+(g.total/maxv*100).toFixed(1)+'%"></i></span><span class="vv">'+money(g.total)+'</span></div>';
    }
    if(groups.size>top.length){
      const rest=[...groups.values()].sort((a,b)=>b.total-a.total).slice(top.length).reduce((s,g)=>s+g.total,0);
      h+='<p class="small" style="margin:8px 0 0">Y '+(groups.size-top.length)+' comercios más por '+money(rest)+'. Los ves todos en Movimientos.</p>';
    }
    h+='</div>';
  }
  if(d.installments.length){
    h+='<div class="block"><h2>Compras a meses</h2><p class="lead">Lo que sigues pagando de compras diferidas.</p>';
    for(const p of d.installments){
      const total=Math.min(p.total_installments,36);let pips='';
      for(let i=1;i<=total;i++)pips+='<i class="'+(i<=p.current_installment?'on':'')+'"></i>';
      const left=p.total_installments-p.current_installment;
      h+='<div class="plan"><div class="plan-top"><b>'+esc(p.description)+'</b><span class="num"><b>'+money(p.required_payment)+'</b> este mes</span></div>'
        +'<div class="pips" aria-hidden="true">'+pips+'</div>'
        +'<div class="small">Mensualidad '+p.current_installment+' de '+p.total_installments+(left===0?', es la última':', faltan '+left)+'. Comprado el '+fmtDate(p.operation_date)+' por '+money(p.original_amount)+', tasa '+pct(p.annual_rate)+'.</div></div>';
    }
    h+='</div>';
  }
  if(d.payment_scenarios.length){
    h+='<div class="block"><h2>Si pagas cada mes</h2><p class="lead">Cuánto tardarías y cuánto pagarías de intereses por tus compras regulares.</p>'
      +'<div class="panel" style="padding:8px 12px"><table><thead><tr><th>Pago mensual</th><th>Terminas en</th><th class="r">Intereses totales</th></tr></thead><tbody>';
    for(const s of d.payment_scenarios){
      const best=num(s.total_interest)===0;
      h+='<tr class="'+(best?'best':'')+'"><td>'+esc(cap(s.label.toLowerCase()))+' <span class="small num">('+money(s.monthly_payment)+')</span></td><td>'+esc(s.months_to_pay)+'</td><td class="r">'+money(s.total_interest)+'</td></tr>';
    }
    h+='</tbody></table></div></div>';
  }
  if(d.warnings&&d.warnings.length){
    h+='<div class="notice"><b>Revisa estos detalles del PDF</b><ul>'+d.warnings.map(w=>'<li>'+esc(w)+'</li>').join('')+'</ul></div>';
  }
  $('#v-resumen').innerHTML=h;
}

/* ---------- Movimientos ---------- */
const FILTERS=[['all','Todos',()=>true],['purchase','Compras',t=>t.kind==='purchase'],['payment','Pagos',t=>t.kind==='payment'],
  ['costs','Intereses e IVA',t=>['interest','tax','fee'].includes(t.kind)],['installment','Mensualidades',t=>t.kind==='installment']];
function renderTx(){
  const d=state.detail;if(!d)return;
  const q=state.q.trim().toLowerCase();
  const pred=FILTERS.find(f=>f[0]===state.filter)[2];
  const rows=d.transactions.filter(t=>pred(t)&&(!state.cat||(state.cat==='sin_clasificar'?t.kind==='purchase'&&(!t.category||t.category==='sin_clasificar'):t.category===state.cat))&&(!q||(t.description+' '+(t.detail||'')).toLowerCase().includes(q)));
  let h='<div class="toolbar">'+FILTERS.map(f=>{
      const n=d.transactions.filter(f[2]).length;
      return '<button class="chip" type="button" data-f="'+f[0]+'" aria-pressed="'+(state.filter===f[0])+'">'+f[1]+'<small>'+n+'</small></button>';
    }).join('')
    +'<select class="search" id="catf" aria-label="Filtrar por categoría"><option value="">Todas las categorías</option>'
    +[...state.categories,{key:'sin_clasificar',label:'Sin clasificar'}].map(c=>'<option value="'+c.key+'"'+(state.cat===c.key?' selected':'')+'>'+esc(c.label)+'</option>').join('')+'</select>'
    +'<input class="search" id="search" type="search" placeholder="Buscar comercio" aria-label="Buscar comercio" value="'+esc(state.q)+'"></div>';
  h+='<div class="tx-wrap"><table><thead><tr><th>Fecha</th><th>Descripción</th><th>Tipo</th><th>Categoría</th><th class="r">Monto</th></tr></thead><tbody>';
  if(!rows.length)h+='<tr><td colspan="5" class="small">No hay movimientos con ese filtro.</td></tr>';
  let net=0;
  for(const t of rows){
    const a=Number(t.amount);net+=a;
    h+='<tr><td class="num" style="white-space:nowrap">'+fmtDate(t.operation_date)+'</td><td>'+esc(t.description)+(t.detail?'<span class="d">'+esc(t.detail)+'</span>':'')+'</td>'
      +'<td><span class="pill '+t.kind+'">'+(KIND[t.kind]||'Otro')+'</span></td>'+'<td>'+catCell(t)+'</td>'
      +'<td class="r '+(a<0?'pos':'')+'">'+(a<0?'−':'')+money(Math.abs(a))+'</td></tr>';
  }
  h+='</tbody></table></div><div class="tx-foot"><span>'+rows.length+' de '+d.transactions.length+' movimientos</span><span>Suma de lo que ves: <b class="num">'+(net<0?'−':'')+money(Math.abs(net))+'</b></span></div>';
  $('#v-movimientos').innerHTML=h;
  const s=$('#search');
  if(document.activeElement&&document.activeElement.id==='search'){s.focus();s.setSelectionRange(s.value.length,s.value.length);}
}
$('#v-movimientos').addEventListener('click',e=>{const c=e.target.closest('.chip');if(c){state.filter=c.dataset.f;renderTx();}});
function catCell(t){
  if(t.kind!=='purchase'||t.id==null)return '';
  const opts=(t.category==='sin_clasificar'||!t.category?'<option value="" selected>Sin clasificar</option>':'')
    +state.categories.map(c=>'<option value="'+c.key+'"'+(t.category===c.key?' selected':'')+'>'+esc(c.label)+'</option>').join('');
  return '<select class="cat-select" data-tx="'+t.id+'" aria-label="Categoría de '+esc(t.description)+'">'+opts+'</select>'
    +(t.deductible?'<span class="tag">posible deducible</span>':'');
}
$('#v-movimientos').addEventListener('change',async e=>{
  if(e.target.id==='catf'){state.cat=e.target.value;renderTx();return;}
  const sel=e.target.closest('.cat-select');if(!sel||!sel.value)return;
  try{
    const r=await api('/transactions/'+sel.dataset.tx+'/category',{method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify({category:sel.value})});
    toast(r.updated>1?'Listo: se aplicó a '+r.updated+' movimientos de ese comercio y a los próximos.':'Categoría guardada; se aplicará a ese comercio en adelante.','ok');
    await select(state.selected,true);
  }catch(ex){toast(ex.message,'bad');}
});
async function loadCategories(){try{state.categories=await api('/categories');}catch(e){state.categories=[];}}
$('#v-movimientos').addEventListener('input',e=>{if(e.target.id==='search'){state.q=e.target.value;const pos=e.target.selectionStart;renderTx();const s=$('#search');s.focus();s.setSelectionRange(pos,pos);}});

/* ---------- Chat ---------- */
const STARTERS=['¿Cuánto debo pagar para no generar intereses y hasta cuándo?','¿En qué gasté más este periodo?','¿Cuánto me costaría pagar solo el mínimo?','¿Cuánto me falta de mis compras a meses?'];
function renderChat(){
  const box=$('#chat');const chat=state.chats[state.selected]||[];
  let h='';
  if(!chat.length&&!state.busy){
    h='<div class="starter"><h2>Pregunta lo que quieras de '+esc(state.detail?monthTitle(state.detail.period_end):'este mes')+'</h2>'
      +'<p class="lead">Respondo con los datos de este estado de cuenta y te indico de dónde salió cada dato.</p><div class="chips">'
      +STARTERS.map(s=>'<button class="chip" type="button" data-q="'+esc(s)+'">'+esc(s)+'</button>').join('')+'</div></div>';
  }
  for(const m of chat){
    if(m.role==='user')h+='<div class="msg user">'+esc(m.text)+'</div>';
    else if(m.role==='error')h+='<div class="msg error"><b>No pude responder.</b> '+esc(m.text)+'</div>';
    else{
      const r=m.meta;let meta='';
      if(r&&r.llm)meta='<div class="msg-meta">'+(r.total_ms/1000).toFixed(1)+' s, '+r.llm.usage.input_tokens+' tokens de entrada y '+r.llm.usage.output_tokens+' de salida, modelo '+esc(r.llm.model)+'</div>';
      let src='';
      if(m.sources&&m.sources.length){
        src='<details><summary>Fuentes ('+m.sources.length+')</summary>'+m.sources.map((s,i)=>'<pre><b>['+(i+1)+'] '+esc(SOURCE[s.kind]||s.kind)+'</b> ('+Math.round(s.score*100)+'% de similitud)\n'+esc(s.content)+'</pre>').join('')+'</details>';
      }
      h+='<div class="msg assistant">'+md(m.text)+meta+src+'</div>';
    }
  }
  if(state.busy)h+='<div class="msg assistant"><span class="typing" aria-label="Escribiendo"><i></i><i></i><i></i></span></div>';
  box.innerHTML=h;box.scrollTop=box.scrollHeight;
  $('#send').disabled=state.busy;
}
async function ask(text){
  const id=state.selected;text=(text||'').trim();
  if(!id||!text||state.busy)return;
  const chat=(state.chats[id]=state.chats[id]||[]);
  const history=buildHistory(chat);
  chat.push({role:'user',text});state.busy=true;$('#q').value='';autosize();renderChat();
  try{
    const r=await api('/ask',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({question:text,statement_id:id,history})});
    chat.push({role:'assistant',text:r.answer,meta:r,sources:r.sources});
  }catch(e){chat.push({role:'error',text:e.message});}
  state.busy=false;
  if(state.selected===id)renderChat();
  if(state.tab==='metricas')loadMetrics();
}
function autosize(){const t=$('#q');t.style.height='auto';t.style.height=Math.min(t.scrollHeight,140)+'px';}
$('#composer').addEventListener('submit',e=>{e.preventDefault();ask($('#q').value);});
$('#q').addEventListener('input',autosize);
$('#q').addEventListener('keydown',e=>{if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();ask($('#q').value);}});
$('#chat').addEventListener('click',e=>{const c=e.target.closest('.chip');if(c)ask(c.dataset.q);});

/* ---------- Métricas ---------- */
async function loadMetrics(){
  const box=$('#v-metricas');
  try{
    const scope=state.user&&state.user.role==='admin'?'all':'me';
    const [stats,recent]=await Promise.all([api('/metrics?scope='+scope),api('/metrics/recent?limit=15&scope='+scope)]);
    const calls=stats.reduce((s,x)=>s+x.calls,0),errors=stats.reduce((s,x)=>s+x.errors,0);
    const tin=stats.reduce((s,x)=>s+x.input_tokens,0),tout=stats.reduce((s,x)=>s+x.output_tokens,0);
    const cost=stats.reduce((s,x)=>s+x.cost_usd,0);
    const chat=stats.find(x=>x.operation==='chat');
    let h=(scope==='all'?'<p class="small">Vista de administrador: métricas de todas las cuentas.</p>':'')+'<div class="strip">'
      +'<div><div class="big">'+calls+'</div><div class="small">llamadas registradas'+(errors?', '+errors+' con error':'')+'</div></div>'
      +'<div><div class="big">'+(chat?(chat.avg_latency_ms/1000).toFixed(1)+' s':'N/D')+'</div><div class="small">tiempo promedio de una respuesta</div></div>'
      +'<div><div class="big">'+(tin+tout).toLocaleString('es-MX')+'</div><div class="small">tokens ('+tin.toLocaleString('es-MX')+' de entrada, '+tout.toLocaleString('es-MX')+' de salida)</div></div>'
      +'<div><div class="big">US$ '+cost.toFixed(4)+'</div><div class="small">costo estimado</div></div></div>';
    h+='<div class="block"><h2>Por operación</h2><div class="tx-wrap" style="margin-top:10px"><table><thead><tr><th>Operación</th><th class="r">Llamadas</th><th class="r">Errores</th><th class="r">Promedio</th><th class="r">p95</th><th class="r">Tokens de entrada</th><th class="r">Tokens de salida</th></tr></thead><tbody>';
    if(!stats.length)h+='<tr><td colspan="7" class="small">Aún no hay llamadas. Haz una pregunta para ver las métricas.</td></tr>';
    for(const s of stats)h+='<tr><td>'+esc(OPS[s.operation]||s.operation)+'</td><td class="r">'+s.calls+'</td><td class="r">'+s.errors+'</td><td class="r">'+Math.round(s.avg_latency_ms)+' ms</td><td class="r">'+Math.round(s.p95_latency_ms)+' ms</td><td class="r">'+s.input_tokens.toLocaleString('es-MX')+'</td><td class="r">'+s.output_tokens.toLocaleString('es-MX')+'</td></tr>';
    h+='</tbody></table></div></div><div class="block"><h2>Últimas llamadas</h2><div class="tx-wrap" style="margin-top:10px"><table><thead><tr><th>Hora</th><th>Operación</th><th>Modelo</th><th>Estado</th><th class="r">Latencia</th><th class="r">Tokens</th></tr></thead><tbody>';
    for(const c of recent)h+='<tr><td class="num" style="white-space:nowrap">'+esc(c.created_at)+'</td><td>'+esc(OPS[c.operation]||c.operation)+'</td><td>'+esc(c.model)+'</td><td><span class="pill '+(c.status==='ok'?'payment':'tax')+'">'+(c.status==='ok'?'Correcta':'Con error')+'</span></td><td class="r">'+Math.round(c.latency_ms)+' ms</td><td class="r">'+(c.input_tokens+c.output_tokens).toLocaleString('es-MX')+'</td></tr>';
    h+='</tbody></table></div></div>';
    box.innerHTML=h;
  }catch(e){box.innerHTML='<p class="small">No se pudieron cargar las métricas: '+esc(e.message)+'</p>';}
}

/* ---------- Pestañas ---------- */
function setTab(name){
  state.tab=name;
  document.querySelectorAll('.tab').forEach(t=>t.setAttribute('aria-selected',String(t.dataset.tab===name)));
  for(const t of ['salud','resumen','movimientos','preguntar','metricas'])$('#v-'+t).hidden=t!==name;
  if(name==='metricas')loadMetrics();
  if(name==='salud')loadHealth();
  if(name==='preguntar'){const c=$('#chat');c.scrollTop=c.scrollHeight;$('#q').focus();}
}
document.querySelector('.tabs').addEventListener('click',e=>{const t=e.target.closest('.tab');if(t)setTab(t.dataset.tab);});

/* ---------- Carga de PDFs ---------- */
const dropHTML=$('#drop').innerHTML;
async function uploadFiles(files){
  const pdfs=[...files].filter(f=>/\.pdf$/i.test(f.name));
  if(!pdfs.length){toast('Elige archivos con extensión .pdf.','bad');return;}
  const drop=$('#drop');drop.classList.add('busy');let last=null;
  for(let i=0;i<pdfs.length;i++){
    drop.innerHTML='<strong>Procesando '+(i+1)+' de '+pdfs.length+'</strong><span>La primera carga puede tardar mientras se prepara el modelo.</span>';
    const fd=new FormData();fd.append('file',pdfs[i]);
    try{
      const r=await api('/upload-pdf/',{method:'POST',body:fd});
      if(r.duplicate){toast(pdfs[i].name+' ya estaba cargado.','info');if(r.statement_id)last=r.statement_id;}
      else{toast(pdfs[i].name+': '+r.n_transactions+' movimientos guardados.','ok');last=r.statement_id;}
      (r.warnings||[]).forEach(w=>toast('Aviso: '+w,'info'));
    }catch(e){toast(pdfs[i].name+': '+e.message,'bad');}
  }
  drop.classList.remove('busy');drop.innerHTML=dropHTML;
  await loadStatements(last);
}
const fileInput=$('#file');
$('#drop').addEventListener('click',()=>fileInput.click());
$('#welcome-btn').addEventListener('click',()=>fileInput.click());
fileInput.addEventListener('change',()=>{if(fileInput.files.length)uploadFiles(fileInput.files);fileInput.value='';});
const drop=$('#drop');
['dragenter','dragover'].forEach(ev=>drop.addEventListener(ev,e=>{e.preventDefault();drop.classList.add('over');}));
['dragleave','drop'].forEach(ev=>drop.addEventListener(ev,e=>{e.preventDefault();drop.classList.remove('over');}));
drop.addEventListener('drop',e=>{if(e.dataTransfer.files.length)uploadFiles(e.dataTransfer.files);});
window.addEventListener('dragover',e=>e.preventDefault());
window.addEventListener('drop',e=>{e.preventDefault();});

/* ---------- Eliminar ---------- */
$('#del').addEventListener('click',async()=>{
  const d=state.detail;if(!d)return;
  if(!confirm('¿Eliminar '+monthTitle(d.period_end)+'? Se borrarán sus movimientos y los datos indexados. No se puede deshacer.'))return;
  try{await api('/statements/'+d.id,{method:'DELETE'});delete state.chats[d.id];toast(monthTitle(d.period_end)+' eliminado.','ok');state.selected=null;await loadStatements();}
  catch(e){toast(e.message,'bad');}
});
