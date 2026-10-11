// Utilidades de formato (fechas, moneda, escape de HTML). Se carga antes de app.js.
const MONTHS=['enero','febrero','marzo','abril','mayo','junio','julio','agosto','septiembre','octubre','noviembre','diciembre'];
const MON=['ene','feb','mar','abr','may','jun','jul','ago','sep','oct','nov','dic'];
const KIND={purchase:'Compra',payment:'Pago',interest:'Interés',tax:'IVA',fee:'Comisión',installment:'Mensualidad',other:'Otro'};
const SOURCE={summary:'Resumen del estado de cuenta',scenarios:'Escenarios de pago',installments:'Compras a meses',transactions:'Movimientos',analytics:'Análisis de gastos'};
const OPS={chat:'Respuestas del modelo',embedding:'Embeddings',retrieval:'Búsqueda en la base',ingestion:'Carga de PDF'};
const mxn=new Intl.NumberFormat('es-MX',{style:'currency',currency:'MXN'});

function esc(s){return String(s==null?'':s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));}
function cap(t){return t.charAt(0).toUpperCase()+t.slice(1);}
function num(v){return v===null||v===undefined||v===''?null:Number(v);}
function money(v){const n=num(v);return n===null||Number.isNaN(n)?'N/D':mxn.format(n);}
function pct(v){const n=num(v);return n===null?'N/D':n.toFixed(2).replace(/\.00$/,'')+'%';}
function parseISO(s){const [y,m,d]=String(s).slice(0,10).split('-').map(Number);return new Date(Date.UTC(y,m-1,d));}
function fmtDate(s,withYear=true){if(!s)return 'N/D';const d=parseISO(s);return d.getUTCDate()+' '+MON[d.getUTCMonth()]+(withYear?' '+d.getUTCFullYear():'');}
function monthName(s){return cap(MONTHS[parseISO(s).getUTCMonth()]);}
function monthTitle(s){return monthName(s)+' '+parseISO(s).getUTCFullYear();}
function daysBetween(a,b){return Math.round((parseISO(b)-parseISO(a))/86400000);}
function todayISO(){const t=new Date();return t.getFullYear()+'-'+String(t.getMonth()+1).padStart(2,'0')+'-'+String(t.getDate()).padStart(2,'0');}

/* Modelo de la barra del ciclo: compras (inicio a corte) y plazo para pagar (corte a límite). */
function cycleModel(startISO,cutISO,dueISO,todayStr){
  const total=Math.max(daysBetween(startISO,dueISO),1);
  const buy=daysBetween(startISO,cutISO);
  const pos=daysBetween(startISO,todayStr);
  let state='past';
  if(pos<0)state='before';else if(pos<=buy)state='buying';else if(pos<=total)state='paying';
  return {buyPct:buy/total*100,payPct:100-buy/total*100,todayPct:Math.min(100,Math.max(0,pos/total*100)),state,daysLeft:total-pos};
}

/* Markdown mínimo y seguro: se escapa todo el HTML antes de dar formato. */
function md(src){
  const inline=s=>s.replace(/\*\*(.+?)\*\*/g,'<strong>$1</strong>').replace(/`(.+?)`/g,'<code>$1</code>').replace(/\[(\d+)\]/g,'<sup class="cite">$1</sup>');
  let html='',list=null,m;
  const close=()=>{if(list){html+='</'+list+'>';list=null;}};
  for(const raw of esc(src).split('\n')){
    const line=raw.trim();
    if(!line){close();continue;}
    if((m=line.match(/^[-*•]\s+(.*)$/))){if(list!=='ul'){close();html+='<ul>';list='ul';}html+='<li>'+inline(m[1])+'</li>';}
    else if((m=line.match(/^\d+[.)]\s+(.*)$/))){if(list!=='ol'){close();html+='<ol>';list='ol';}html+='<li>'+inline(m[1])+'</li>';}
    else if((m=line.match(/^#{1,4}\s+(.*)$/))){close();html+='<h4>'+inline(m[1])+'</h4>';}
    else{close();html+='<p>'+inline(line)+'</p>';}
  }
  close();return html;
}

/* Solo se envían a la API pares completos pregunta/respuesta (los errores no cuentan). */
function buildHistory(chat){
  const h=[];
  for(let i=0;i<chat.length-1;i++){
    if(chat[i].role==='user'&&chat[i+1].role==='assistant')h.push({role:'user',content:chat[i].text},{role:'assistant',content:chat[i+1].text});
  }
  return h;
}
