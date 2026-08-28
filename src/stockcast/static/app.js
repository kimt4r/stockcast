const $ = (selector) => document.querySelector(selector);
const csrf = $('meta[name="csrf-token"]')?.content || '';
let side = 'buy';
let lastChartValues = [];

function money(value) { return `${Number(value || 0).toLocaleString('ko-KR')}원`; }
function toast(message) {
  const node = $('#toast');
  node.textContent = message;
  node.classList.add('show');
  setTimeout(() => node.classList.remove('show'), 3000);
}
function showError(message) {
  const node = $('#api-error');
  if (node) { node.textContent = message; node.hidden = false; }
  toast(message);
}
function clearError() {
  const node = $('#api-error');
  if (node) { node.textContent = ''; node.hidden = true; }
}
async function api(url, options = {}) {
  const response = await fetch(url, { ...options, headers: { 'Content-Type':'application/json', 'X-CSRF-Token':csrf, ...(options.headers || {}) }});
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.error || '요청을 처리하지 못했습니다.');
  return data;
}
function drawChart(values) {
  lastChartValues = values;
  const canvas = $('#chart');
  const empty = $('#empty-chart');
  if (!canvas || values.length < 2) return;
  empty.style.display = 'none';
  const ratio = window.devicePixelRatio || 1;
  const rect = canvas.getBoundingClientRect();
  canvas.width = rect.width * ratio; canvas.height = rect.height * ratio;
  const ctx = canvas.getContext('2d'); ctx.scale(ratio, ratio);
  const w = rect.width, h = rect.height, pad = 18;
  const min = Math.min(...values), max = Math.max(...values), range = max - min || 1;
  const pts = values.map((v,i) => [pad + i*(w-pad*2)/(values.length-1), h-pad-(v-min)*(h-pad*2)/range]);
  ctx.strokeStyle='#e4e6df';ctx.lineWidth=1;
  for(let i=1;i<4;i++){const y=i*h/4;ctx.beginPath();ctx.moveTo(0,y);ctx.lineTo(w,y);ctx.stroke()}
  const gradient=ctx.createLinearGradient(0,0,0,h);gradient.addColorStop(0,'rgba(70,121,91,.28)');gradient.addColorStop(1,'rgba(70,121,91,0)');
  ctx.beginPath();ctx.moveTo(pts[0][0],h);pts.forEach(p=>ctx.lineTo(p[0],p[1]));ctx.lineTo(pts.at(-1)[0],h);ctx.closePath();ctx.fillStyle=gradient;ctx.fill();
  ctx.beginPath();pts.forEach((p,i)=>i?ctx.lineTo(p[0],p[1]):ctx.moveTo(p[0],p[1]));ctx.strokeStyle='#245c43';ctx.lineWidth=2;ctx.stroke();
  const last=pts.at(-1);ctx.beginPath();ctx.arc(last[0],last[1],4,0,Math.PI*2);ctx.fillStyle='#d9f06a';ctx.fill();ctx.strokeStyle='#245c43';ctx.stroke();
}
async function loadSymbol() {
  const symbol = $('#symbol').value.trim();
  if (!/^\d{6}$/.test(symbol)) return toast('6자리 종목코드를 입력해주세요.');
  $('#chart-state').textContent='조회 중';
  try {
    clearError();
    const quote = await api(`/api/quote/${symbol}`);
    $('#price').textContent=money(quote.price);$('#symbol-label').textContent=symbol;$('#order-price').placeholder=quote.price;
    $('#signal').textContent=quote.signal;$('#signal').className=quote.signal.toLowerCase();
    $('#chart-state').textContent=`최근 ${quote.closes.length}일`;drawChart(quote.closes);
  } catch(e) { $('#chart-state').textContent='오류';showError(`시세 조회 실패: ${e.message}`); }
}
$('#search')?.addEventListener('click', loadSymbol);
$('#symbol')?.addEventListener('keydown', e => { if(e.key==='Enter') loadSymbol(); });
document.querySelectorAll('.side-toggle button').forEach(button => button.addEventListener('click', () => {
  document.querySelectorAll('.side-toggle button').forEach(x=>x.classList.remove('active'));button.classList.add('active');side=button.dataset.side;
}));
$('#preview-order')?.addEventListener('click', async () => {
  const payload={side,symbol:$('#symbol').value.trim(),quantity:Number($('#quantity').value),price:Number($('#order-price').value),execute:false};
  try { const data=await api('/api/order',{method:'POST',body:JSON.stringify(payload)});$('#order-result').textContent=`${side==='buy'?'매수':'매도'} ${payload.symbol} ${payload.quantity}주 · ${payload.price?money(payload.price):'시장가'} — ${data.message}`;toast('안전 한도와 주문 조건을 확인했습니다.'); }
  catch(e){$('#order-result').textContent=e.message;toast(e.message)}
});
async function loadBalance() {
  const button = $('#load-balance');
  if (button) { button.disabled = true; button.textContent = '조회 중'; }
  try {
    clearError();
    const data=await api('/api/balance');
    const rows=data.output1||[];
    const summary=(data.output2||[])[0]||{};
    $('#balance-body').innerHTML=rows.length?rows.map(r=>`<tr><td>${r.prdt_name||r.pdno||'-'}</td><td>${Number(r.hldg_qty||0).toLocaleString()}</td><td>${money(r.pchs_avg_pric)}</td><td>${money(r.prpr)}</td><td>${money(r.evlu_pfls_amt)}</td></tr>`).join(''):'<tr><td colspan="5" class="empty-row">보유 종목이 없습니다.</td></tr>';
    $('#cash-total').textContent=money(summary.dnca_tot_amt);
    $('#evaluation-total').textContent=money(summary.tot_evlu_amt);
    $('#profit-total').textContent=money(summary.evlu_pfls_smtl_amt);
  } catch(e) { showError(`잔고 조회 실패: ${e.message}`); }
  finally { if (button) { button.disabled = false; button.textContent = '잔고 새로고침'; } }
}
$('#load-balance')?.addEventListener('click', loadBalance);
$('#save-settings')?.addEventListener('click', async () => {
  const button = $('#save-settings');
  const payload = {
    allowed_symbols: $('#settings-symbols').value.trim(),
    max_order_krw: Number($('#settings-max-order').value),
    allow_live: $('#settings-allow-live')?.checked === true,
  };
  button.disabled = true;
  try {
    clearError();
    await api('/api/settings', {method:'POST', body:JSON.stringify(payload)});
    $('#settings-result').textContent = '설정을 인증 파일에 저장했습니다.';
    toast('주문 안전 설정을 저장했습니다.');
  } catch(e) { showError(`설정 저장 실패: ${e.message}`); }
  finally { button.disabled = false; }
});
function renderAutotrade(status) {
  const state = $('#autotrade-state');
  if (!state) return;
  state.textContent = status.running ? '실행 중' : '중지됨';
  state.className = `pill ${status.running ? 'running' : ''}`;
  if ($('#start-autotrade')) $('#start-autotrade').disabled = status.running;
  if ($('#stop-autotrade')) $('#stop-autotrade').disabled = !status.running;
  const events = status.events || [];
  if ($('#autotrade-events')) {
    $('#autotrade-events').replaceChildren(...(events.length ? events.map(event => {
      const row=document.createElement('div');row.className=`auto-event ${event.kind}`;
      const time=document.createElement('time');time.textContent=event.time.replace('T',' ');
      const message=document.createElement('span');message.textContent=event.message;
      row.append(time,message);return row;
    }) : [Object.assign(document.createElement('p'),{textContent:'실행 기록이 없습니다.'})]));
  }
}
async function loadAutotrade() {
  if (!$('#autotrade-state')) return;
  try { renderAutotrade(await api('/api/autotrade')); } catch(e) { showError(`자동매매 상태 조회 실패: ${e.message}`); }
}
async function loadDiscovery() {
  const list=$('#discovery-list');
  if (!list) return;
  const button=$('#refresh-discovery');
  list.replaceChildren(Object.assign(document.createElement('p'),{textContent:'시장 종목을 검색하고 있습니다.'}));
  if (button) button.disabled=true;
  try {
    clearError();
    const limit=Number($('#auto-scan-limit')?.value||10);
    const data=await api(`/api/autotrade/discover?limit=${limit}`);
    const items=data.items||[];
    list.replaceChildren(...(items.length ? items.map(item => {
      const row=document.createElement('button');row.type='button';row.className='discovery-row';
      const name=document.createElement('strong');name.textContent=item.name;
      const symbol=document.createElement('span');symbol.textContent=item.symbol;
      const price=document.createElement('span');price.textContent=money(item.price);
      row.append(name,symbol,price);
      row.addEventListener('click',()=>{ $('#symbol').value=item.symbol;loadSymbol();window.scrollTo({top:0,behavior:'smooth'}); });
      return row;
    }) : [Object.assign(document.createElement('p'),{textContent:'검색된 종목이 없습니다.'})]));
  } catch(e) { list.replaceChildren(Object.assign(document.createElement('p'),{textContent:'검색에 실패했습니다.'}));showError(`종목 자동검색 실패: ${e.message}`); }
  finally { if(button) button.disabled=false; }
}
$('#refresh-discovery')?.addEventListener('click',loadDiscovery);
$('#start-autotrade')?.addEventListener('click', async () => {
  const payload={symbols:$('#auto-symbols').value,auto_discover:$('#auto-discover').checked,scan_limit:Number($('#auto-scan-limit').value),select_count:Number($('#auto-select-count').value),quantity:Number($('#auto-quantity').value),interval_seconds:Number($('#auto-interval').value),max_positions:Number($('#auto-max-positions').value),stop_loss_pct:Number($('#auto-stop-loss').value),take_profit_pct:Number($('#auto-take-profit').value)};
  try { clearError();renderAutotrade(await api('/api/autotrade/start',{method:'POST',body:JSON.stringify(payload)}));toast('모의투자 자동매매를 시작했습니다.'); }
  catch(e){showError(`자동매매 시작 실패: ${e.message}`)}
});
$('#stop-autotrade')?.addEventListener('click', async () => {
  try { renderAutotrade(await api('/api/autotrade/stop',{method:'POST'}));toast('자동매매를 중지했습니다.'); }
  catch(e){showError(`자동매매 중지 실패: ${e.message}`)}
});
$('#logout')?.addEventListener('click', async () => { try{await api('/logout',{method:'POST'});location.href='/';}catch(e){toast(e.message)} });
window.addEventListener('resize',()=>{ if (lastChartValues.length) drawChart(lastChartValues); });
window.addEventListener('DOMContentLoaded', () => { loadSymbol(); setTimeout(loadBalance, 1200); setTimeout(loadDiscovery, 2400); loadAutotrade(); });
setInterval(loadAutotrade, 5000);
