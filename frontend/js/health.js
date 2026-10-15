// Vista "Salud crediticia": índice, indicadores con semáforo, recomendaciones y gráficas (SVG sin librerías).
// Todo lo calcula el servidor (/credit-health); aquí solo se dibuja. Textos de datos → esc() o textContent.

const STATUS={good:{icon:'✓',text:'Bien'},warn:{icon:'!',text:'Atención'},bad:{icon:'✕',text:'Riesgo'},na:{icon:'–',text:'Sin datos'}};
const pctRound=v=>v==null?'N/D':Math.round(v)+'%';

async function loadHealth(){
  const box=$('#v-salud');
  if(!state.selected){box.innerHTML='';return;}
  box.classList.add('loading');
  try{
    const r=await api('/credit-health?statement_id='+state.selected);
    renderHealth(r);
  }catch(e){box.innerHTML='<p class="small">No se pudo calcular tu salud crediticia: '+esc(e.message)+'</p>';}
  finally{box.classList.remove('loading');}
}

function scoreStatus(score){return score==null?'na':score>=80?'good':score>=60?'warn':'bad';}

function renderHealth(r){
  const st=scoreStatus(r.score);
  let h='<section class="hero-health">'
    +'<div><div class="hero-label">Índice de salud crediticia</div>'
    +'<div class="hero-num">'+(r.score==null?'—':r.score)+'<span>/100</span></div>'
    +'<div class="status-chip '+st+'"><span aria-hidden="true">'+STATUS[st].icon+'</span>'+esc(r.level)+'</div></div>'
    +'<div class="meter" role="img" aria-label="Índice '+(r.score==null?'sin datos':r.score+' de 100')+'">'
    +'<div class="meter-fill '+st+'" style="width:'+(r.score||0)+'%"></div></div>'
    +'<p class="small">'+esc(r.notes[0]||'')+'</p></section>';

  h+='<div class="ind-grid">';
  for(const i of r.indicators){
    const s=STATUS[i.status];
    h+='<article class="ind"><div class="ind-head"><span class="ind-label">'+esc(i.label)+'</span>'
      +'<span class="status-chip '+i.status+'"><span aria-hidden="true">'+s.icon+'</span>'+s.text+'</span></div>'
      +'<div class="ind-value">'+esc(i.display)+'</div><p class="small">'+esc(i.detail)+'</p></article>';
  }
  h+='</div>';

  if(r.recommendations.length){
    h+='<div class="block"><h2>Qué puedes hacer</h2><ol class="recs">'+r.recommendations.map(x=>'<li>'+esc(x)+'</li>').join('')+'</ol></div>';
  }

  h+='<div class="grid2"><div class="block"><h2>Uso del crédito por mes</h2><div class="chart" id="ch-util"></div></div>'
    +'<div class="block"><h2>Saldo por mes</h2><p class="small">Pago para no generar intereses al cierre de cada periodo.</p><div class="chart" id="ch-bal"></div></div></div>';

  h+='<div class="block"><h2>Gasto por categoría</h2>';
  if(r.essential_share!=null)h+='<p class="small">Esencial: <b>'+pctRound(r.essential_share)+'</b> · discrecional: <b>'+pctRound(100-r.essential_share)+'</b>'
    +(Number(r.deductible_total)>0?' · posibles deducibles (salud y educación): <b>'+money(r.deductible_total)+'</b>, pide tu CFDI':'')+'</p>';
  h+='<div class="cat-bars">';
  const max=Math.max(...r.categories.map(c=>Number(c.amount)),1);
  for(const c of r.categories){
    const w=Math.max(Number(c.amount)/max*100,1.5);
    h+='<div class="cat-row" title="'+esc(c.label)+': '+c.count+' compra(s)"><span class="cat-name">'+esc(c.label)
      +(c.deductible?' <span class="tag">posible deducible</span>':'')+'</span>'
      +'<span class="cat-track"><span class="cat-bar'+(c.category==='sin_clasificar'?' muted':'')+'" style="width:'+w+'%"></span>'
      +'<span class="cat-val">'+money(c.amount)+' <small>'+pctRound(c.share)+'</small></span></span></div>';
  }
  if(!r.categories.length)h+='<p class="small">No hay compras en este periodo.</p>';
  h+='</div></div>';

  h+='<details class="block table-view"><summary>Ver los datos mensuales en tabla</summary><div class="tx-wrap"><table><thead><tr>'
    +'<th>Cierre</th><th class="r">Uso del crédito</th><th class="r">Saldo</th><th class="r">Compras</th><th class="r">Pagos</th><th class="r">Intereses y comisiones</th><th class="r">Índice</th></tr></thead><tbody>'
    +r.series.map(p=>'<tr><td>'+fmtDate(p.period_end)+'</td><td class="r">'+pctRound(p.utilization)+'</td><td class="r">'+money(p.balance)+'</td><td class="r">'+money(p.purchases)
      +'</td><td class="r">'+money(p.payments)+'</td><td class="r">'+money(p.finance_cost)+'</td><td class="r">'+(p.score==null?'N/D':p.score)+'</td></tr>').join('')
    +'</tbody></table></div></details>';
  if(r.notes.length>1)h+='<div class="notice">'+r.notes.slice(1).map(n=>'<p>'+esc(n)+'</p>').join('')+'</div>';

  $('#v-salud').innerHTML=h;
  const months=r.series.map(p=>monthName(p.period_end).slice(0,3)+' '+String(parseISO(p.period_end).getUTCFullYear()).slice(2));
  lineChart($('#ch-util'),months,r.series.map(p=>p.utilization),{max:100,ref:30,refLabel:'30% recomendado',fmt:pctRound,name:'Uso del crédito'});
  columnChart($('#ch-bal'),months,r.series.map(p=>p.balance==null?null:Number(p.balance)),{fmt:money,name:'Saldo'});
}

/* ---------- gráficas ---------- */
const W=560,H=220,M={t:16,r:56,b:28,l:44};

function tooltip(box){
  let t=box.querySelector('.tip');
  if(!t){t=document.createElement('div');t.className='tip';t.hidden=true;box.appendChild(t);}
  return t;
}
function showTip(box,x,y,value,label){
  const t=tooltip(box);t.replaceChildren();
  const v=document.createElement('strong');v.textContent=value;
  const l=document.createElement('span');l.textContent=label;
  t.append(v,l);t.hidden=false;
  const bw=box.clientWidth,sx=bw/W;
  t.style.left=Math.min(Math.max(x*sx-60,0),bw-130)+'px';t.style.top=Math.max(y*(box.clientHeight/H)-58,0)+'px';
}

function lineChart(box,labels,values,o){
  const n=values.length,iw=W-M.l-M.r,ih=H-M.t-M.b;
  const X=i=>M.l+(n===1?iw/2:i*iw/(n-1)),Y=v=>M.t+ih-(v/o.max)*ih;
  let s='<svg viewBox="0 0 '+W+' '+H+'" role="img" aria-label="'+esc(o.name)+' por mes" tabindex="0">';
  for(const g of [0,50,100])s+='<line class="grid" x1="'+M.l+'" x2="'+(W-M.r)+'" y1="'+Y(g)+'" y2="'+Y(g)+'"/><text class="axis" x="'+(M.l-8)+'" y="'+(Y(g)+4)+'" text-anchor="end">'+g+'%</text>';
  s+='<line class="ref" x1="'+M.l+'" x2="'+(W-M.r)+'" y1="'+Y(o.ref)+'" y2="'+Y(o.ref)+'"/><text class="axis" x="'+(W-M.r+6)+'" y="'+(Y(o.ref)+4)+'">'+esc(o.refLabel.split(' ')[0])+'</text>';
  const pts=values.map((v,i)=>v==null?null:[X(i),Y(v)]).filter(Boolean);
  if(pts.length>1){
    s+='<path class="area" d="M'+pts[0][0]+','+Y(0)+' L'+pts.map(p=>p.join(',')).join(' L')+' L'+pts[pts.length-1][0]+','+Y(0)+'Z"/>';
    s+='<path class="line" d="M'+pts.map(p=>p.join(',')).join(' L')+'"/>';
  }
  const last=values.length-1;
  if(values[last]!=null)s+='<circle class="dot" cx="'+X(last)+'" cy="'+Y(values[last])+'" r="4.5"/><text class="end" x="'+(X(last)+9)+'" y="'+(Y(values[last])+4)+'">'+o.fmt(values[last])+'</text>';
  labels.forEach((l,i)=>{if(n<=6||i%Math.ceil(n/6)===0||i===last)s+='<text class="axis" x="'+X(i)+'" y="'+(H-8)+'" text-anchor="middle">'+esc(l)+'</text>';});
  s+='<line class="cross" y1="'+M.t+'" y2="'+(M.t+ih)+'" visibility="hidden"/><rect class="hit" x="'+M.l+'" y="0" width="'+iw+'" height="'+H+'"/></svg>';
  box.innerHTML=s+(n<2?'<p class="small">Carga más meses para ver la tendencia.</p>':'');
  const svg=box.querySelector('svg'),cross=svg.querySelector('.cross');
  const at=i=>{cross.setAttribute('x1',X(i));cross.setAttribute('x2',X(i));cross.setAttribute('visibility','visible');
    showTip(box,X(i),values[i]==null?M.t:Y(values[i]),o.fmt(values[i]),o.name+' · '+labels[i]);};
  const nearest=ev=>{const r=svg.getBoundingClientRect(),x=(ev.clientX-r.left)*W/r.width;
    return n===1?0:Math.max(0,Math.min(n-1,Math.round((x-M.l)/(iw/(n-1)))));};
  svg.addEventListener('pointermove',ev=>at(nearest(ev)));
  svg.addEventListener('pointerleave',()=>{cross.setAttribute('visibility','hidden');tooltip(box).hidden=true;});
  let k=last;
  svg.addEventListener('focus',()=>at(k));
  svg.addEventListener('blur',()=>{cross.setAttribute('visibility','hidden');tooltip(box).hidden=true;});
  svg.addEventListener('keydown',ev=>{if(ev.key==='ArrowLeft')k=Math.max(0,k-1);else if(ev.key==='ArrowRight')k=Math.min(n-1,k+1);else return;ev.preventDefault();at(k);});
}

function columnChart(box,labels,values,o){
  const n=values.length,iw=W-M.l-M.r,ih=H-M.t-M.b;
  const max=Math.max(...values.map(v=>v||0),1)*1.1;
  const band=iw/n,bw=Math.min(24,band*0.6);
  const X=i=>M.l+band*i+band/2,Y=v=>M.t+ih-(v/max)*ih;
  const tick=v=>v>=1000?'$'+Math.round(v/1000)+'K':'$'+Math.round(v);
  let s='<svg viewBox="0 0 '+W+' '+H+'" role="img" aria-label="'+esc(o.name)+' por mes">';
  for(const g of [0,max/2,max])s+='<line class="grid" x1="'+M.l+'" x2="'+(W-M.r)+'" y1="'+Y(g)+'" y2="'+Y(g)+'"/><text class="axis" x="'+(M.l-8)+'" y="'+(Y(g)+4)+'" text-anchor="end">'+tick(g)+'</text>';
  values.forEach((v,i)=>{
    if(v==null||v<=0)return;
    const x=X(i)-bw/2,y=Y(v),h=Y(0)-y,r=Math.min(4,h);
    s+='<path class="col'+(i===n-1?' current':'')+'" data-i="'+i+'" tabindex="0" d="M'+x+','+Y(0)+' V'+(y+r)+' Q'+x+','+y+' '+(x+r)+','+y+' H'+(x+bw-r)+' Q'+(x+bw)+','+y+' '+(x+bw)+','+(y+r)+' V'+Y(0)+' Z"/>';
  });
  if(values[n-1])s+='<text class="end" x="'+X(n-1)+'" y="'+(Y(values[n-1])-6)+'" text-anchor="middle">'+o.fmt(values[n-1])+'</text>';
  labels.forEach((l,i)=>{if(n<=8||i%Math.ceil(n/8)===0||i===n-1)s+='<text class="axis" x="'+X(i)+'" y="'+(H-8)+'" text-anchor="middle">'+esc(l)+'</text>';});
  box.innerHTML=s+'</svg>';
  const show=el=>{const i=Number(el.dataset.i);showTip(box,X(i),Y(values[i]),o.fmt(values[i]),o.name+' · '+labels[i]);};
  const hide=()=>{tooltip(box).hidden=true;};
  box.querySelectorAll('.col').forEach(el=>{
    el.addEventListener('pointerenter',()=>show(el));el.addEventListener('focus',()=>show(el));
    el.addEventListener('pointerleave',hide);el.addEventListener('blur',hide);
  });
}
