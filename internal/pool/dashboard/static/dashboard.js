let lastState=null,lastKey=null,selectedChain='Blakecoin',mkgenWorker='rig1';
const $=id=>document.getElementById(id);
const ADDR_TYPE_LABEL={bech32:'BECH32',p2sh:'P2SH',legacy:'LEGACY',miningkey:'MINING KEY',none:'UNKNOWN'};
const ADDR_TYPE_TOOLTIP={bech32:'Native SegWit bech32 payout address',p2sh:'Wrapped SegWit P2SH-P2WPKH payout address',legacy:'Legacy P2PKH payout address',miningkey:'40-hex mining key; payout script type is set by pool configuration',none:'No recognizable payout address or mining key'};
function esc(v){return String(v??'-').replace(/[&<>"']/g,m=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[m]))}
function stat(label,value,cls=''){return '<div class="stat"><span class="label">'+esc(label)+'</span><span class="value '+cls+'">'+esc(value)+'</span></div>'}
function age(s){s=Number(s||0); if(s<60)return s+'s ago'; if(s<3600)return Math.floor(s/60)+'m ago'; if(s<86400)return Math.floor(s/3600)+'h ago'; return Math.floor(s/86400)+'d ago'}
function lastShareAge(s){s=Number(s||0); return s<2?'live':age(s)}
function ageTime(v){if(!v)return '-'; let t=typeof v==='number'?v*1000:Date.parse(v); if(!Number.isFinite(t))return '-'; return age(Math.max(0,Math.floor((Date.now()-t)/1000)))}
function shortHash(s,n=18){s=String(s||''); return s.length>n?s.slice(0,n)+'...':s}
function chainByName(name){return (lastState?.chains||[]).find(c=>c.label===name)||(lastState?.chains||[])[0]||{}}
function chainByTicker(t){return (lastState?.chains||[]).find(c=>c.ticker===t)||{}}
function ticker(label){return (lastState?.chain_tickers||{})[label]||label}
function currentMkgenWorker(){return (mkgenWorker||'rig1').trim()||'rig1'}
function fallbackCopyText(v){
  const el=document.createElement('textarea');
  el.value=String(v??'');
  el.setAttribute('readonly','');
  el.style.position='fixed';
  el.style.left='-9999px';
  el.style.top='0';
  document.body.appendChild(el);
  el.focus();
  el.select();
  el.setSelectionRange(0,el.value.length);
  let ok=false;
  try{ok=document.execCommand('copy')}finally{document.body.removeChild(el)}
  if(!ok) throw new Error('copy command failed');
}
function copyFeedback(btn,ok){
  if(!btn) return;
  const old=btn.dataset.copyLabel||btn.textContent;
  btn.dataset.copyLabel=old;
  btn.textContent=ok?'copied':'copy failed';
  btn.classList.toggle('copied',ok);
  btn.classList.toggle('copy-failed',!ok);
  setTimeout(()=>{btn.textContent=old; btn.classList.remove('copied','copy-failed')},1000);
}
async function copyText(v,btn){
  const text=String(v??'');
  if(!text){copyFeedback(btn,false); return false}
  try{
    if(window.isSecureContext&&navigator.clipboard?.writeText) await navigator.clipboard.writeText(text);
    else fallbackCopyText(text);
    copyFeedback(btn,true);
    return true;
  }catch(e){
    try{fallbackCopyText(text); copyFeedback(btn,true); return true}
    catch(err){console.warn('copy failed',err); copyFeedback(btn,false); return false}
  }
}
function bindCopyButtons(){document.querySelectorAll('[data-copy]').forEach(b=>b.onclick=()=>copyText(b.dataset.copy,b))}
function solveEvents(){return (lastState?.recent_solves||lastState?.proxy?.recent_solves||[])}
function stateClass(state){return state==='mining'?'good':state==='idle'?'blue':'warn'}
function chainSyncLabel(c){
  if(!c || c.status==='offline') return 'Offline';
  if(c.status==='synced') return 'Synced';
  return 'Syncing';
}
function chipForOutcome(c){const cls=c.accepted?'accepted':(c.attempted?'rejected':'skipped'); return '<span class="share-chain-badge '+cls+'" title="'+esc(c.label||c.alias||c.ticker)+'">'+esc(c.ticker||c.alias||c.label)+'</span>'}
function renderOutcomeChips(chains){chains=chains||[]; return chains.length?'<div class="share-chain-results">'+chains.map(chipForOutcome).join('')+'</div>':'<span class="muted">—</span>'}
function renderAddrType(t){t=t||'none'; return '<span class="type-pill '+esc(t)+'" title="'+esc(ADDR_TYPE_TOOLTIP[t]||'')+'">'+esc(ADDR_TYPE_LABEL[t]||t)+'</span>'}
function overview(label,value,sub='',cls=''){return '<div class="overview-card"><span class="label">'+esc(label)+'</span><span class="value '+cls+'">'+esc(value)+'</span>'+(sub?'<div class="subvalue">'+sub+'</div>':'')+'</div>'}
function solveTotals(){
  const totals={parent:0,aux:0};
  for(const ev of solveEvents()){
    for(const c of ev.chains||[]){
      if(!c.accepted) continue;
      const tick=c.ticker||c.alias||c.label;
      if(tick==='BLC') totals.parent++; else totals.aux++;
    }
  }
  return totals;
}
function recentAuxWin(maxAgeSec=3600){
  const now=Date.now();
  return solveEvents().some(ev=>{
    const ts=Date.parse(ev.time||'');
    if(!Number.isFinite(ts) || Math.floor((now-ts)/1000)>maxAgeSec) return false;
    return (ev.chains||[]).some(c=>c.accepted&&(c.ticker||c.alias||c.label)!=='BLC');
  });
}
function renderMineHelp(){
  const st=lastState?.stratum||{}, url='stratum+tcp://'+(st.host||location.hostname)+':'+(st.port||3334);
  $('mineHelp').innerHTML='<div class="two"><div class="box"><pre>BAIKAL GIANT-B ASIC\n\nPOOL URL    '+esc(url)+'\nALGORITHM   Blake256r8\nUSER        miningkey.worker\nPASS        x\nEXTRANONCE  DISABLE  (uncheck the Extranonce box)</pre></div><div class="box"><pre>GPU MINING\n\nCGMINER\ncgminer --blake256 -o '+esc(url)+' -u miningkey.worker -p x\n\nSGMINER\nsgminer --no-submit-stale --kernel blakecoin --gpu-platform 0 -I 30 --no-extranonce \\\n  -o '+esc(url)+' -u miningkey.worker -p x</pre></div></div><p class="muted"><b class="blue">User:</b> use your generated mining key. Add <span class="mono">.worker</span> if you want a worker label.</p>';
}
function renderChains(){
  const chains=lastState?.chains||[];
  $('chainChips').innerHTML=chains.map(c=>'<button class="chip '+(c.status==='synced'?'good':c.status==='syncing'?'':'bad')+(c.label===selectedChain?' active':'')+'" data-chain="'+esc(c.label)+'">'+esc(c.ticker)+'</button>').join('');
  document.querySelectorAll('[data-chain]').forEach(b=>b.onclick=()=>{selectedChain=b.dataset.chain; renderChains()});
  const c=chainByName(selectedChain);
  $('chainStats').innerHTML=[
    stat('Coin',c.label||'-','blue'), stat('Blocks',(c.blocks||0).toLocaleString(),'blue'), stat('Headers',(c.headers||0).toLocaleString()),
    stat('Peers',c.connections||0), stat('Sync',chainSyncLabel(c),c.status==='synced'?'good':(c.status==='offline'?'bad':'warn')), stat('Difficulty',c.difficulty||'-')
  ].join('')+'<div class="box" style="grid-column:1/-1"><span class="label">Best</span><span class="mono">'+esc(shortHash(c.best_hash,64))+'</span>'+(c.error?'<div class="bad">'+esc(c.error)+'</div>':'')+'</div>';
}
function renderPool(){
  const p=lastState?.pool||{}, x=lastState?.proxy||{}, sub=x.submissions||{}, state=lastState?.status?.state||'starting';
  const ready=(x.ready_count??0)+' / '+(x.total_chains??0);
  const wins=solveTotals();
  const recentWins=wins.parent+' BLC / '+wins.aux+' aux';
  $('poolStats').innerHTML=[
    stat('Status',state,stateClass(state)), stat('Merged Wins',recentWins,wins.parent||wins.aux?'good':'muted'), stat('Miners',p.miners||0),
    stat('Accepted',p.accepted||0,'good'), stat('Rejected',p.rejected||0,Number(p.rejected)>0?'bad':''), stat('Aux RPC',ready,x.ready_count===x.total_chains?'good':'warn'),
    stat('Gotwork',p.gotwork_sent||0,'blue'), stat('Skipped',p.gotwork_skipped||0,'muted'), stat('Aux Accepted',wins.aux,wins.aux?'good':'')
  ].join('');
  const attempts=Number(sub.attempts||0), accepted=Number(sub.accepted||0), stale=Number(sub.stale||0), failed=Number(sub.failed||0);
  const warning=attempts>=12&&accepted===0&&(failed>0||stale>0)&&!recentAuxWin();
  const notice=$('notice'); notice.className='notice'+(warning?' show':'');
  notice.textContent=warning?'Aux submit warning: repeated gotwork submits have not produced a recent aux accept. Review gotwork debug before production use.':'';
}
function renderPayouts(){
  const rows=lastState?.chains||[];
  if(!rows.length){$('payouts').innerHTML='<div class="empty">no wallet data available yet</div>'; return}
  $('payouts').innerHTML='<div class="payout-totals-grid">'+rows.map(c=>{
    const w=c.wallet||{}, bal=Number(w.balance||0), imm=Number(w.immature||0), unc=Number(w.unconfirmed||0);
    return '<div class="payout-total-tile"><div class="payout-total-meta"><span class="name">'+esc(c.label)+'</span><span class="chip">'+esc(c.ticker)+'</span></div><div class="payout-total-amounts">'
      + '<div class="payout-total-box"><span class="lbl">pool wallet</span><div class="amount '+(bal>0?'blue':'muted')+'">'+esc(w.balance||'0')+'</div></div>'
      + '<div class="payout-total-box"><span class="lbl">immature</span><div class="amount '+(imm>0?'warn':'muted')+'">'+esc(w.immature||'0')+'</div></div>'
      + '<div class="payout-total-box"><span class="lbl">unconfirmed</span><div class="amount '+(unc>0?'warn':'muted')+'">'+esc(w.unconfirmed||'0')+'</div></div>'
      + '</div></div>'
  }).join('')+'</div>';
}
function captureOpen(selector, keyAttr){
  const map=new Map();
  document.querySelectorAll(selector).forEach(el=>{const key=el.getAttribute(keyAttr); if(key)map.set(key,el.open)});
  return map;
}
function restoreOpen(selector, keyAttr, map){
  document.querySelectorAll(selector).forEach(el=>{const key=el.getAttribute(keyAttr); if(key&&map.has(key))el.open=map.get(key)});
}
function payoutRows(m){
  const rows=m.payouts&&m.payouts.length?m.payouts:(lastState.chain_order||[]).map(l=>({label:l,ticker:ticker(l),blocks:0,amount:'0.00000000'}));
  return rows.map(p=>{
    const amount=Number(p.amount||0);
    const amountCls=amount>0?'blue':'muted';
    const est=p.estimated?' est':'';
    const symbol=p.ticker||ticker(p.label);
    return '<div class="payout-line"><span class="payout-ticker"><span class="chip">'+esc(symbol)+'</span></span><small class="payout-shares muted">| shares '+esc(p.blocks||0)+'</small><b class="payout-amount '+amountCls+'">'+esc(p.amount||'0.00000000')+' '+esc(symbol)+esc(est)+'</b></div>';
  }).join('');
}
function payoutSolvedBlocks(m){
  const rows=m.payouts&&m.payouts.length?m.payouts:null;
  if(!rows) return Number(m.blocks||0);
  const total=rows.reduce((sum,p)=>sum+Number(p.blocks||0),0);
  return total>0?total:Number(m.blocks||0);
}
function renderMiners(){
  const miners=lastState?.miners||[];
  const active=miners.filter(m=>m.active).length;
  $('minerCount').textContent=active+' active · '+miners.length+' total';
  if(!miners.length){$('miners').innerHTML='<div class="empty">no stratum activity yet — copy the URL above and point a miner here</div>'; return}
  const openRows=captureOpen('.id-row','data-row-key');
  $('miners').innerHTML='<div class="id-list">'+miners.map((m,i)=>{
    const t=m.addr_type||'none', worker=m.worker||'—', addr=m.mining_key||m.addr||m.raw_username||m.username, topIdentity=m.raw_username||m.username||addr;
    const solvedBlocks=payoutSolvedBlocks(m);
    const rowKey=(m.raw_username||m.username||'')+'|'+(m.remote||'');
    const detailAddr=m.mining_key?'<dt>mining key</dt><dd class="mono">'+esc(m.mining_key)+'<span class="copy-id" data-copy="'+esc(m.mining_key)+'">copy</span></dd>':'<dt>address</dt><dd class="mono">'+esc(m.addr||'not parsed')+(m.addr?'<span class="copy-id" data-copy="'+esc(m.addr)+'">copy</span>':'')+'</dd>';
    return '<details class="id-row" data-row-key="'+esc(rowKey)+'"><summary>'
      + '<span>▸</span><span class="dot '+(m.active?'active':'')+'"></span>'
      + '<span class="identity"><span class="addr-mono '+esc(t)+'">'+esc(topIdentity)+'</span></span>'
      + '<span class="id-stats"><span><b>'+esc(m.shares)+'</b>shares</span><span><b>'+esc(m.accepted_shares||m.shares||0)+'</b>accepted</span><span><b>'+esc(solvedBlocks)+'</b>blocks</span><span><b>'+esc(lastShareAge(m.age))+'</b>last</span><span><b>'+esc(m.remote)+'</b>peer</span></span>'
      + '</summary><div class="id-detail"><div class="id-detail-grid"><dl>'
      + '<dt>worker name</dt><dd>'+esc(worker)+'</dd>'+detailAddr+'<dt>address type</dt><dd>'+renderAddrType(t)+'</dd><dt>raw username</dt><dd class="mono">'+esc(m.raw_username||m.username)+'</dd><dt>peer</dt><dd class="mono">'+esc(m.remote)+'</dd><dt>state</dt><dd>'+(m.active?'<span class="good">active</span>':'<span class="muted">idle</span>')+'</dd>'
      + '</dl><dl><dt>accepted shares</dt><dd>'+esc(m.accepted_shares||m.shares||0)+' counted toward payout / round contribution</dd><dt>blocks solved</dt><dd><b class="good">'+esc(solvedBlocks)+'</b> across all chains</dd><dt>all payouts</dt><dd><div class="payout-list">'+payoutRows(m)+'</div></dd></dl></div></div></details>'
  }).join('')+'</div>';
  restoreOpen('.id-row','data-row-key',openRows);
  bindCopyButtons();
}
function renderSolvedBlocks(){
  const rows=solveEvents().slice().reverse();
  $('solveCount').textContent=rows.length;
  const latest=rows[0];
  $('solveLatest').innerHTML=latest?'latest: <span class="mono latest-hash">'+esc(latest.parent_hash||'')+'</span> <span>'+ageTime(latest.time)+'</span> '+renderOutcomeChips(latest.chains||[]):'';
  if(!rows.length){$('solvedBlocks').innerHTML='<div class="empty">no block candidates recorded yet — accepted/rejected chain chips appear here when a share reaches a parent or aux target</div>'; return}
  $('solvedBlocks').innerHTML='<table class="recent-table solved-table solved-blocks-table"><thead><tr><th class="age-col">age</th><th class="miner-col">miner</th><th class="type-col">type</th><th class="chains-col">chains</th><th class="count-col">accepted</th><th class="count-col">rejected</th></tr></thead><tbody>'+rows.map(ev=>{const id=parseUserDisplay(ev.username||''); const known=Boolean(ev.username); return '<tr><td class="age-col">'+ageTime(ev.time)+'</td><td class="miner-col mono">'+(known?esc(ev.username):'<span class="muted">—</span>')+'</td><td class="type-col">'+(known?renderAddrType(id.type):'<span class="muted">—</span>')+'</td><td class="chains-col">'+renderOutcomeChips(ev.chains)+'</td><td class="count-col good">'+esc(ev.accepted_count||0)+'</td><td class="count-col '+((ev.rejected_count||0)>0?'bad':'muted')+'">'+esc(ev.rejected_count||0)+'</td></tr>'}).join('')+'</tbody></table>';
}
function parseUserDisplay(u){let key=u,worker=''; const p=String(u||'').lastIndexOf('.'); if(p>0){key=u.slice(0,p); worker=u.slice(p+1)} let type='none'; if(/^[0-9a-fA-F]{40}$/.test(key))type='miningkey'; else if(/^[a-z]+1[qpzry9x8gf2tvdw0s3jn54khce6mua7l]+$/.test(key))type='bech32'; else if(/^[13q][123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz]{25,61}$/.test(key))type=key[0]==='3'||key[0]==='q'?'p2sh':'legacy'; return {key,worker,type}}
function renderShares(){
  const shares=(lastState?.recent_shares||[]).slice().reverse();
  const shareCount=$('shareCount');
  if(shareCount) shareCount.textContent=shares.length;
  $('shares').innerHTML=shares.length?shares.map(s=>'<tr><td>'+ageTime(s.time)+'</td><td>'+esc(s.username)+'</td><td>'+esc(s.worker||'—')+'</td><td>'+renderAddrType(s.addr_type||'none')+'</td><td class="share-hash mono">'+esc(s.hash)+'</td></tr>').join(''):'<tr><td colspan="5">No shares yet</td></tr>';
}
function renderPoolLog(){
  const lines=lastState?.pool_log||[];
  $('poolLogCount').textContent=lines.length;
  $('poolLog').textContent=lines.length?lines.join('\n'):'No pool log file found yet.';
}
function renderMkgen(){
  const root=$('mkgen');
  if(!lastKey){
    root.innerHTML='<div class="mkgen-empty"><div class="mkgen-empty-cols"><div class="mkgen-empty-col"><div class="mkgen-empty-col-title">Mining Key</div><div class="mkgen-empty-col-desc">Use the bare <span class="mono">&lt;40hex&gt;[.worker]</span> mining key in your miner. The pool derives bech32 payout addresses for BlakeStream 25.2 chains.</div><button id="genKey" class="primary">Generate Mining Key</button></div><div class="mkgen-empty-col"><div class="mkgen-empty-col-title">Miner Username</div><div class="mkgen-empty-col-desc">Baikal and GPU miners use <span class="mono">&lt;miningkey&gt;.rig1</span> as the username and <span class="mono">x</span> as the password.</div></div></div></div>';
  } else {
    const b=lastKey, addrs=b.derived_addresses||{}, st=lastState?.stratum||{}, stratumUrl='stratum+tcp://'+(st.host||location.hostname)+':'+(st.port||3334);
    const rows=orderedAddressEntries(addrs);
    root.innerHTML='<div class="mkgen-result"><div class="mkgen-cols"><div class="mkgen-col mkgen-col-left">'
      + '<div class="mkgen-row danger"><div class="lbl"><span>private key - save this, it cannot be recovered</span><button class="copy-btn" data-copy="'+esc(b.private_key)+'">copy</button></div><div class="val">'+esc(b.private_key)+'</div></div>'
      + '<div class="mkgen-row"><div class="lbl"><span>public key</span><button class="copy-btn" data-copy="'+esc(b.public_key)+'">copy</button></div><div class="val">'+esc(b.public_key)+'</div></div>'
      + '<div class="mkgen-row"><div class="lbl"><span>mining key</span><button class="copy-btn" data-copy="'+esc(b.mining_key)+'">copy</button></div><div class="val address">'+esc(b.mining_key)+'</div></div>'
      + '<div class="mkgen-row mkgen-cmd-row"><div class="lbl"><span>cgminer command - copy and paste into your miner config</span><button class="copy-btn" id="copyCmd">copy command</button></div><div class="form"><label for="mkgenWorker">worker name:</label><input id="mkgenWorker" type="text" value="'+esc(currentMkgenWorker())+'" placeholder="rig1"></div><div class="cmd" id="mkgenCmd">cgminer -o '+esc(stratumUrl)+' \\<br>&nbsp;&nbsp;&nbsp;&nbsp;-u '+esc(b.mining_key)+'.<span id="mkgenWorkerName">'+esc(currentMkgenWorker())+'</span> -p x</div></div>'
      + '<div class="mkgen-actions"><button id="saveKey" class="save">Save keys to file</button><button id="copyUser">Copy Username</button><button id="genKey" class="primary">Generate Mining Key</button></div>'
      + '</div><div class="mkgen-col mkgen-col-right"><div class="mkgen-row mkgen-col-header"><div class="lbl"><span>derived payout addresses - bech32</span></div></div>'
      + '<div class="mkgen-col-scroll">'
      + rows.map(([label,d])=>'<div class="mkgen-row"><div class="lbl"><span>'+esc(label)+' <span class="muted">('+esc(d.hrp||ticker(label).toLowerCase())+'1...)</span></span><button class="copy-btn" data-copy="'+esc(d.address||'')+'">copy</button></div><div class="val address-derived">'+esc(d.address||'')+'</div></div>').join('')
      + '</div></div></div></div>';
  }
  const gen=$('genKey'); if(gen) gen.onclick=async()=>{const r=await fetch('/api/generate-mining-key',{method:'POST'}).then(r=>r.json()); if(r.ok){lastKey=r.bundle; renderMkgen()}};
  const cu=$('copyUser'); if(cu&&lastKey) cu.onclick=()=>copyText(lastKey.mining_key+'.'+currentMkgenWorker(),cu);
  const sk=$('saveKey'); if(sk&&lastKey) sk.onclick=()=>{const lines=['Private key for wallets: '+lastKey.private_key,'Public key: '+lastKey.public_key,'Mining key payload: '+lastKey.mining_key,'Stratum username for miner: '+lastKey.mining_key+'.'+currentMkgenWorker(),'','Derived payout addresses:']; const entries=orderedAddressEntries(lastKey.derived_addresses||{}); for(const [label,d] of entries) lines.push('- '+label+' ['+d.hrp+']: '+d.address); const blob=new Blob([lines.join('\n')+'\n'],{type:'text/plain'}); const a=document.createElement('a'); a.href=URL.createObjectURL(blob); a.download='mining_keys.txt'; a.click(); setTimeout(()=>URL.revokeObjectURL(a.href),1000)};
  const worker=$('mkgenWorker'), workerName=$('mkgenWorkerName'); if(worker&&workerName) worker.oninput=()=>{mkgenWorker=worker.value.trim()||'rig1'; workerName.textContent=currentMkgenWorker()};
  const cc=$('copyCmd'); if(cc&&lastKey) cc.onclick=()=>{const st=lastState?.stratum||{}; copyText('cgminer -o stratum+tcp://'+(st.host||location.hostname)+':'+(st.port||3334)+' -u '+lastKey.mining_key+'.'+currentMkgenWorker()+' -p x',cc)};
  bindCopyButtons();
}
function orderedAddressEntries(addrs){
  const order=lastState?.chain_order||Object.keys(addrs||{});
  const used=new Set(), rows=[];
  for(const label of order){if(addrs&&addrs[label]){rows.push([label,addrs[label]]); used.add(label)}}
  for(const entry of Object.entries(addrs||{})){if(!used.has(entry[0])) rows.push(entry)}
  return rows;
}
async function refreshLastKeyAddresses(){
  if(!lastKey?.mining_key) return;
  const want=Object.keys(lastState?.mining_key_hrps||{});
  if(!want.length) return;
  const have=lastKey.derived_addresses||{};
  if(want.every(label=>have[label])) return;
  try{
    const r=await fetch('/api/derive-addresses-v2',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({mining_key:lastKey.mining_key})}).then(r=>r.json());
    if(r.ok&&r.derived_addresses) lastKey.derived_addresses=r.derived_addresses;
  }catch(e){}
}
async function refresh(){
  lastState=await fetch('/api/state').then(r=>r.json());
  await refreshLastKeyAddresses();
  const st=lastState.stratum||{}, stratumUrl='stratum+tcp://'+(st.host||location.hostname)+':'+(st.port||3334);
  $('stratumUrl').textContent=stratumUrl;
  $('copyStratum').onclick=()=>copyText(stratumUrl,$('copyStratum'));
  renderMineHelp(); renderChains(); renderPool(); renderPayouts(); renderMiners(); renderSolvedBlocks(); renderShares(); renderPoolLog(); renderMkgen();
}
refresh(); setInterval(refresh,5000);
