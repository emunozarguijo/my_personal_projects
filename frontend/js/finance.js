// Vista "Mi mes": registro manual de ingresos y gastos en efectivo + panorama del mes calendario
// (efectivo y tarjeta Nu) con el índice de salud financiera. Los cálculos los hace el servidor (/finance/month).
// Textos capturados por la persona → siempre con esc().

const fin={month:null,mode:'cash',data:null};
const INCOME_KIND={salario:'💼 Salario',honorarios:'🧾 Honorarios',otros:'✨ Otros ingresos'};
const SOURCE_LABEL={efectivo:'💵 Efectivo',nu:'💳 Tarjeta Nu',ingreso:'💰 Ingreso'};

function ym(d){return d.getFullYear()+'-'+String(d.getMonth()+1).padStart(2,'0');}
function shiftMonth(m,delta){const [y,mo]=m.split('-').map(Number);return ym(new Date(y,mo-1+delta,1));}
function monthEnd(m){const [y,mo]=m.split('-').map(Number);const d=new Date(y,mo,0);return m+'-'+String(d.getDate()).padStart(2,'0');}
function defaultDate(){const t=todayISO();return t.slice(0,7)===fin.month?t:fin.month+'-01';}

async function loadMonth(){
  if(!fin.month)fin.month=ym(new Date());
  const box=$('#v-mes');box.classList.add('loading');
  try{fin.data=await api('/finance/month?month='+fin.month);renderMonth();}
  catch(e){box.innerHTML='<p class="small">No se pudo cargar el mes: '+esc(e.message)+'</p>';}
  finally{box.classList.remove('loading');}
}

function formHTML(){
  const cats=catOptions(null);
  const cash=fin.mode==='cash';
  return '<form class="quick-add" id="quick-add" novalidate>'
    +'<div class="seg" role="group" aria-label="Tipo de registro">'
    +'<button type="button" data-mode="cash" aria-pressed="'+cash+'"><span class="ti" aria-hidden="true">💵</span>Gasto en efectivo</button>'
    +'<button type="button" data-mode="income" aria-pressed="'+!cash+'"><span class="ti" aria-hidden="true">💰</span>Ingreso</button></div>'
    +'<div class="qa-grid">'
    +'<label>Monto<input id="qa-amount" inputmode="decimal" autocomplete="off" placeholder="0.00" required></label>'
    +'<label>Descripción<input id="qa-desc" maxlength="120" placeholder="'+(cash?'Ej. Mercado, taxi, farmacia':'Ej. Sueldo quincena')+'"></label>'
    +(cash
      ?'<label>Categoría<select id="qa-cat"><option value="">🪄 Automática</option>'+cats+'</select></label>'
      :'<label>Tipo<select id="qa-kind">'+Object.entries(INCOME_KIND).map(([k,v])=>'<option value="'+k+'">'+v+'</option>').join('')+'</select></label>')
    +'<label>Fecha<input id="qa-date" type="date" value="'+defaultDate()+'" required></label>'
    +'</div>'
    +(cash?'':'<label class="check"><input type="checkbox" id="qa-rec"> Se repite cada mes</label>')
    +'<p class="auth-error" id="qa-error" role="alert" hidden></p>'
    +'<button class="btn" type="submit">'+(cash?'Agregar gasto':'Agregar ingreso')+'</button></form>';
}

function tile(label,value,sub){return '<div><div class="big">'+value+'</div><div class="small">'+label+(sub?'<br>'+sub:'')+'</div></div>';}

function renderMonth(){
  const d=fin.data,st=scoreStatus(d.score);
  let h='<div class="month-nav"><button class="icon-btn" type="button" data-nav="-1" aria-label="Mes anterior">‹</button>'
    +'<h2>'+esc(monthTitle(d.month+'-01'))+'</h2>'
    +'<button class="icon-btn" type="button" data-nav="1" aria-label="Mes siguiente">›</button></div>';
  h+=formHTML();

  h+='<section class="hero-health">'
    +'<div><div class="hero-label">Índice de salud financiera</div>'
    +'<div class="hero-num">'+(d.score==null?'—':d.score)+'<span>/100</span></div>'
    +'<div class="status-chip '+st+'"><span aria-hidden="true">'+STATUS[st].icon+'</span>'+esc(d.level)+'</div></div>'
    +'<div class="meter" role="img" aria-label="Índice '+(d.score==null?'sin datos':d.score+' de 100')+'"><div class="meter-fill '+st+'" style="width:'+(d.score||0)+'%"></div></div>'
    +'<p class="small">'+esc(d.notes[0]||'')+'</p></section>';

  const bal=Number(d.balance);
  h+='<div class="strip month-strip">'
    +tile('Ingresos',money(d.income_total))
    +tile('Gastos',money(d.expense_total),'efectivo '+money(d.cash_total)+' · Nu '+money(d.nu_total)+(Number(d.finance_cost)>0?' · intereses '+money(d.finance_cost):''))
    +tile(bal<0?'Te faltó':'Te sobró',(bal<0?'−':'')+money(Math.abs(bal)))
    +tile('Tasa de ahorro',d.savings_rate==null?'N/D':Math.round(d.savings_rate)+'%')
    +'</div>';
  if(Number(d.card_payments)>0)h+='<p class="small">Pagaste '+money(d.card_payments)+' a tu tarjeta Nu este mes. No cuenta como gasto: tus compras ya se contaron cuando las hiciste.</p>';

  h+='<div class="ind-grid">'+d.indicators.map(i=>{const s=STATUS[i.status];
    return '<article class="ind"><div class="ind-head"><span class="ind-label">'+esc(i.label)+'</span><span class="status-chip '+i.status+'"><span aria-hidden="true">'+s.icon+'</span>'+s.text+'</span></div>'
      +'<div class="ind-value">'+esc(i.display)+'</div><p class="small">'+esc(i.detail)+'</p></article>';}).join('')+'</div>';

  if(d.recommendations.length)h+='<div class="block"><h2>Qué puedes hacer</h2><ol class="recs">'+d.recommendations.map(x=>'<li>'+esc(x)+'</li>').join('')+'</ol></div>';

  h+='<div class="block"><h2>Gasto por categoría</h2><div class="cat-bars">';
  const max=Math.max(...d.categories.map(c=>Number(c.total)),1);
  for(const c of d.categories){
    h+='<div class="cat-row"><span class="cat-name">'+catBadge(c.category)+esc(c.label)+(c.deductible?' <span class="tag">posible deducible</span>':'')+'</span>'
      +'<span class="cat-track"><span class="cat-bar'+(c.category==='sin_clasificar'?' muted':'')+'" style="width:'+Math.max(Number(c.total)/max*100,1.5)+'%"></span>'
      +'<span class="cat-val">'+money(c.total)+' <small>efectivo '+money(c.cash)+' · Nu '+money(c.nu)+'</small></span></span></div>';
  }
  if(!d.categories.length)h+='<p class="small">Aún no hay gastos este mes.</p>';
  h+='</div></div>';

  h+='<div class="block"><h2>Movimientos del mes</h2><div class="tx-wrap"><table><thead><tr><th>Fecha</th><th>Descripción</th><th>Origen</th><th>Categoría</th><th class="r">Monto</th><th></th></tr></thead><tbody>';
  if(!d.movements.length)h+='<tr><td colspan="6" class="small">Sin movimientos. Agrega tu ingreso y tus gastos en efectivo arriba.</td></tr>';
  for(const m of d.movements){
    const inc=m.source==='ingreso';
    let cat='';
    if(m.source==='efectivo')cat='<span class="cat-cell">'+catBadge(m.category)+'<select class="cat-select" data-cash="'+m.id+'" aria-label="Categoría de '+esc(m.description)+'">'
      +(m.category==='sin_clasificar'?'<option value="" selected>❔ Sin clasificar</option>':'')+catOptions(m.category)+'</select></span>';
    else if(m.source==='nu')cat='<span class="cat-cell">'+catBadge(m.category)+esc((state.categories.find(c=>c.key===m.category)||{label:'Sin clasificar'}).label)+'</span>';
    let act='';
    if(m.source==='efectivo')act='<button class="link-btn" type="button" data-del-cash="'+m.id+'">Eliminar</button>';
    if(inc)act=(m.recurring?'<button class="link-btn" type="button" data-stop="'+m.id+'">Ya no se repite</button> ':'')+'<button class="link-btn" type="button" data-del-inc="'+m.id+'"'+(m.recurring?' data-rec="1"':'')+'>Eliminar</button>';
    h+='<tr><td class="num" style="white-space:nowrap">'+fmtDate(m.date)+'</td><td>'+esc(m.description)+(m.recurring?'<span class="d">Se repite cada mes</span>':'')+'</td>'
      +'<td><span class="pill src-'+m.source+'">'+SOURCE_LABEL[m.source]+'</span></td><td>'+cat+'</td>'
      +'<td class="r '+(inc?'pos':'')+'">'+(inc?'+':'')+money(m.amount)+'</td><td class="r">'+act+'</td></tr>';
  }
  h+='</tbody></table></div></div>';
  if(d.notes.length>1)h+='<div class="notice">'+d.notes.slice(1).map(n=>'<p>'+esc(n)+'</p>').join('')+'</div>';
  $('#v-mes').innerHTML=h;
}

/* ---------- eventos ---------- */
$('#v-mes').addEventListener('click',async e=>{
  const nav=e.target.closest('[data-nav]');
  if(nav){fin.month=shiftMonth(fin.month,Number(nav.dataset.nav));return loadMonth();}
  const seg=e.target.closest('.seg button');
  if(seg){fin.mode=seg.dataset.mode;const keep=$('#qa-amount').value;renderMonth();$('#qa-amount').value=keep;$('#qa-amount').focus();return;}
  const b=e.target.closest('button');if(!b)return;
  try{
    if(b.dataset.delCash){if(!confirm('¿Eliminar este gasto en efectivo?'))return;await api('/cash-expenses/'+b.dataset.delCash,{method:'DELETE'});}
    else if(b.dataset.delInc){if(!confirm(b.dataset.rec?'¿Eliminar este ingreso de TODOS los meses? Si solo dejó de llegar, usa "Ya no se repite".':'¿Eliminar este ingreso?'))return;
      await api('/incomes/'+b.dataset.delInc,{method:'DELETE'});}
    else if(b.dataset.stop){await api('/incomes/'+b.dataset.stop,{method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify({end_date:monthEnd(fin.month)})});
      toast('Listo: este ingreso ya no se contará después de '+monthTitle(fin.month+'-01')+'.','ok');}
    else return;
    await loadMonth();
  }catch(ex){toast(ex.message,'bad');}
});

$('#v-mes').addEventListener('change',async e=>{
  const sel=e.target.closest('.cat-select[data-cash]');if(!sel||!sel.value)return;
  try{await api('/cash-expenses/'+sel.dataset.cash,{method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify({category:sel.value})});await loadMonth();}
  catch(ex){toast(ex.message,'bad');}
});

$('#v-mes').addEventListener('submit',async e=>{
  if(e.target.id!=='quick-add')return;
  e.preventDefault();
  const err=$('#qa-error'),raw=$('#qa-amount').value.replace(/[$,\s]/g,''),amount=Number(raw),date=$('#qa-date').value;
  if(!raw||!(amount>0))return showError(err,'Escribe un monto mayor a cero.');
  if(!/^\d+(\.\d{1,2})?$/.test(raw))return showError(err,'El monto admite hasta dos decimales.');
  if(!date)return showError(err,'Elige la fecha.');
  const desc=$('#qa-desc').value.trim();
  const btn=e.target.querySelector('button[type=submit]');btn.disabled=true;
  try{
    if(fin.mode==='cash'){
      const body={date,amount:raw,description:desc};const cat=$('#qa-cat').value;if(cat)body.category=cat;
      const r=await api('/cash-expenses',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
      const label=(state.categories.find(c=>c.key===r.category)||{label:'Sin clasificar'}).label;
      toast('Gasto agregado: '+money(r.amount)+' en '+label+'.','ok');
    }else{
      await api('/incomes',{method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({date,amount:raw,kind:$('#qa-kind').value,description:desc,recurring:$('#qa-rec').checked})});
      toast('Ingreso agregado.','ok');
    }
    if(date.slice(0,7)!==fin.month)fin.month=date.slice(0,7);
    await loadMonth();
    $('#qa-amount').focus();
  }catch(ex){showError(err,ex.message);}
  finally{btn.disabled=false;}
});
