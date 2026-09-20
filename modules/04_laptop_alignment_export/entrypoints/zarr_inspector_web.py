#!/usr/bin/env python3
"""Start the standalone dynamic Zarr inspector web app."""

from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from urllib.request import urlopen

from _bootstrap import add_module_root_to_path

add_module_root_to_path()

from umift_laptop_alignment.pipeline.inspect.catalog import ZarrCatalog, deletion_blockers


HTML = r'''<!doctype html>
<html><head><meta charset="utf-8"><title>UMI-FT Zarr Inspector</title>
<style>body{font:14px system-ui;margin:0;color:#20252b;background:#f5f6f7}header{display:flex;gap:10px;align-items:center;padding:10px 16px;border-bottom:1px solid #d9dde2;background:#fff}header strong{margin-right:8px;white-space:nowrap}header select{min-width:150px;max-width:300px}select,button{padding:7px 9px;border:1px solid #c8ced6;background:#fff}button.active{background:#20252b;color:#fff}button:disabled{opacity:.5;cursor:not-allowed}#frame{min-width:180px}.workspace{height:calc(100vh - 116px);display:grid;grid-template-columns:minmax(0,1.45fr) minmax(430px,1fr);grid-template-rows:minmax(0,.46fr) minmax(0,.54fr);gap:10px;padding:10px;box-sizing:border-box}.surface{min-width:0;min-height:0;background:#fff;border:1px solid #d9dde2;display:flex;flex-direction:column}.global-surface{grid-row:1 / 3}.surface-head{height:38px;display:flex;align-items:center;justify-content:space-between;padding:0 10px;border-bottom:1px solid #e5e7eb;font-weight:600}.mode{display:flex;gap:4px}.media{flex:1;min-height:0;display:flex;align-items:center;justify-content:center;background:#111;overflow:hidden}.media img{width:100%;height:100%;object-fit:contain}.coin-body{padding:8px 12px 10px;min-height:0;display:flex;flex-direction:column}.muted{color:#68717a}.sync-ok{color:#067647}.sync-warn{color:#b54708}table{width:100%;font-size:13px;text-align:right;border-collapse:collapse;margin:5px 0}th,td{padding:3px 5px;border-bottom:1px solid #e5e7eb;white-space:nowrap}canvas{width:100%;height:100%;min-height:230px}.cleanup-bar{height:58px;box-sizing:border-box;border-top:1px solid #d9dde2;background:#fff;padding:10px 16px;display:flex;align-items:center;justify-content:space-between;gap:16px}.cleanup-bar span{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.danger{border-color:#d92d20;color:#b42318;background:#fff}.danger:hover{background:#fff1f0}</style></head>
<body><header><strong>UMI-FT Inspector</strong><select id="datasetRuns" aria-label="Dataset"></select><select id="datasets" aria-label="Zarr"></select><select id="episodes" aria-label="Episode"></select><button id="play">Play</button><button id="pause">Pause</button><input id="frame" type="range" min="0" value="0"><span id="frameNo">0</span></header>
<main class="workspace"><section class="surface global-surface"><div class="surface-head"><span>Global Camera</span><div class="mode"><button id="globalRgbMode" class="active">RGB</button><button id="globalDepthMode">Depth</button></div></div><div class="media"><img id="globalMedia"></div></section>
<section class="surface"><div class="surface-head"><span>Gripper Camera</span><div class="mode"><button id="gripperRgbMode" class="active">RGB</button><button id="gripperDepthMode">Depth</button></div></div><div class="media"><img id="gripperMedia"></div></section>
<section class="surface"><div class="surface-head"><span>Readings from CoinFT</span><span id="coinTime" class="muted"></span></div><div class="coin-body"><table><thead><tr><th>External Fx</th><th>External Fy</th><th>External Fz</th><th>Internal Grasp</th></tr></thead><tbody><tr><td id="externalFx"></td><td id="externalFy"></td><td id="externalFz"></td><td id="graspForce"></td></tr></tbody></table><canvas id="coinPlot" width="900" height="390"></canvas></div></section></main>
<footer class="cleanup-bar"><span id="datasetMeta" class="muted"></span><div><button id="deleteEpisode">Delete Episode + raw</button> <button id="deleteZarr" class="danger">Delete Zarr + all episodes</button></div></footer>
<script>
let runDatasets=[], catalog=[], current=null, timer=null, forceCache={}, forcePlotScale=20, globalMode='rgb', gripperMode='rgb';
const $=id=>document.getElementById(id);
async function get(url){let r=await fetch(url);if(!r.ok)throw Error(await r.text());return r.json()}
async function post(url,payload){let r=await fetch(url,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});if(!r.ok)throw Error(await r.text());return r.json()}
function setOptions(id,rows){let select=$(id);select.replaceChildren(...rows.map(row=>{let option=document.createElement('option');option.value=row.value;option.textContent=row.label;return option}))}
function runDataset(){let value=$('datasetRuns').value;return value===''?null:runDatasets[+value]}
function dataset(){let value=$('datasets').value;return value===''?null:catalog[+value]}
function clearPlayback(message){clearInterval(timer);timer=null;current=null;forceCache={};$('globalMedia').removeAttribute('src');$('gripperMedia').removeAttribute('src');$('coinPlot').getContext('2d').clearRect(0,0,$('coinPlot').width,$('coinPlot').height);$('frame').value=0;$('frame').max=0;$('frame').disabled=true;$('frameNo').textContent='0';$('play').disabled=true;$('pause').disabled=true;$('deleteEpisode').disabled=true;$('coinTime').textContent=message||'';$('coinTime').className='muted';for(const id of ['externalFx','externalFy','externalFz','graspForce'])$(id).textContent='--'}
async function loadZarrs(preferredPath='',preferredEpisode=''){let run=runDataset();if(!run){setOptions('datasets',[{value:'',label:'No Zarr'}]);$('datasets').disabled=true;$('episodes').disabled=true;$('deleteZarr').disabled=true;$('datasetMeta').textContent='No Datasets';clearPlayback('Select a Dataset');return}let rows=catalog.map((item,index)=>({item,index})).filter(row=>row.item.run_dir===run.run_dir);setOptions('datasets',rows.length?rows.map(row=>({value:String(row.index),label:'Zarr: '+row.item.path.split('/').pop()})):[{value:'',label:'No Zarr yet'}]);$('datasets').disabled=!rows.length;$('deleteZarr').disabled=false;if(rows.length){let preferred=rows.find(row=>row.item.path===preferredPath);$('datasets').value=String((preferred||rows[0]).index);await loadEpisodes(preferredEpisode)}else{$('episodes').disabled=true;setOptions('episodes',[{value:'',label:'No Episode'}]);$('datasetMeta').textContent=`${run.display_name} | ${run.episode_count} source episode(s) | No Zarr yet`;clearPlayback('No Zarr generated for this Dataset yet')}}
async function loadEpisodes(preferredEpisode=''){let d=dataset();if(!d){clearPlayback('No Zarr selected');return}let rows=d.episodes||[];setOptions('episodes',rows.length?rows.map(e=>({value:e.name,label:'Episode: '+(e.display_name||e.name)})):[{value:'',label:'No Episode'}]);$('episodes').disabled=!rows.length;$('deleteZarr').disabled=false;if(rows.length){let preferred=rows.find(e=>e.name===preferredEpisode);$('episodes').value=(preferred||rows[rows.length-1]).name;await loadEpisode()}else{$('datasetMeta').textContent=(d.run_id||'run')+' | Zarr has no episodes';clearPlayback('Zarr has no episodes')}}
async function loadEpisode(){let d=dataset();let e=$('episodes').value;if(!d||!e){clearPlayback('No Episode selected');return}current=await get('/api/episode?dataset='+encodeURIComponent(d.path)+'&episode='+encodeURIComponent(e));let arrays=current.episode.arrays;let n=(arrays.rgb_0||Object.values(arrays).find(x=>x.shape.length>0))?.shape[0]||1;$('frame').max=Math.max(0,n-1);$('frame').value=0;$('frame').disabled=false;$('play').disabled=false;$('pause').disabled=false;$('deleteEpisode').disabled=false;$('datasetMeta').textContent=[runDataset()?.display_name,d.path.split('/').pop(),current.episode.display_name,current.episode.session_id].filter(Boolean).join(' | ');forceCache={};forcePlotScale=20;await loadForceData();render();}
function mediaUrl(field,i){return '/api/frame?dataset='+encodeURIComponent(dataset().path)+'&episode='+encodeURIComponent($('episodes').value)+'&field='+field+'&index='+i+'&t='+Date.now()}
function render(){if(!current)return;let i=+$('frame').value;$('frameNo').textContent=i;let arrays=current.episode.arrays,globalField=globalMode==='depth'&&arrays.depth_0?'depth_0':'rgb_global_0',gripperField=gripperMode==='depth'&&arrays.iphone_depth_0?'iphone_depth_0':'rgb_0';$('globalMedia').src=mediaUrl(globalField,i);$('gripperMedia').src=mediaUrl(gripperField,i);drawForceCharts()}
async function loadForceData(){let base='?dataset='+encodeURIComponent(dataset().path)+'&episode='+encodeURIComponent($('episodes').value)+'&count=5000';let names=['wrench_left_0','wrench_right_0','wrench_time_stamps_0','rgb_time_stamps_0'];for(const name of names){try{forceCache[name]=(await get('/api/series'+base+'&field='+name)).values}catch(e){forceCache[name]=[]}}}
function currentTime(){let t=forceCache.rgb_time_stamps_0||[],i=+$('frame').value;return Number((t[i]||[0])[0]||0)}
function nearestCoinIndex(time){let t=forceCache.wrench_time_stamps_0||[];if(!t.length)return -1;let best=0,bestDelta=Infinity;for(let i=0;i<t.length;i++){let delta=Math.abs(Number((t[i]||[0])[0])-time);if(delta<bestDelta){best=i;bestDelta=delta}}return best}
function fmt(v){return Number.isFinite(v)?Number(v).toFixed(5):'--'}
function combinedSample(index){let left=forceCache.wrench_left_0?.[index]||[],right=forceCache.wrench_right_0?.[index]||[];return [Number(left[0]||0)+Number(right[0]||0),Number(left[1]||0)+Number(right[1]||0),Number(left[2]||0)+Number(right[2]||0),(-Number(left[0]||0)+Number(right[0]||0))/2]}
function updateCoinTable(time){let rawIndex=nearestCoinIndex(time),times=forceCache.wrench_time_stamps_0||[],sampleTime=rawIndex<0?NaN:Number((times[rawIndex]||[0])[0]),delta=sampleTime-time,valid=rawIndex>=0&&Number.isFinite(delta)&&Math.abs(delta)<=0.1,i=valid?rawIndex:-1,v=i<0?[]:combinedSample(i);$('coinTime').innerHTML=`RGB time <b>${time.toFixed(4)} s</b> | CoinFT time <b>${valid?sampleTime.toFixed(4):'--'} s</b> | Δt <b>${valid?(delta*1000).toFixed(1):'--'} ms</b>`;$('coinTime').className=valid?'sync-ok':'sync-warn';$('externalFx').textContent=fmt(Number(v[0]));$('externalFy').textContent=fmt(Number(v[1]));$('externalFz').textContent=fmt(Number(v[2]));$('graspForce').textContent=fmt(Number(v[3]))}
function drawForceCharts(){let time=currentTime();updateCoinTable(time);drawCoinPlot(time)}
function niceScale(value){if(!Number.isFinite(value)||value<=0)return 1;let p=10**Math.floor(Math.log10(value)),s=value/p;return (s<=1?1:s<=2?2:s<=5?5:10)*p}
function drawCoinPlot(time){let c=$('coinPlot'),x=c.getContext('2d'),times=forceCache.wrench_time_stamps_0||[],points=times.map((t,i)=>({t:Number((t||[0])[0]),v:combinedSample(i)})).filter(p=>Number.isFinite(p.t)),rgbTimes=forceCache.rgb_time_stamps_0||[];x.clearRect(0,0,c.width,c.height);if(!points.length||!rgbTimes.length)return;let windowSeconds=10,lo=time-windowSeconds/2,hi=time+windowSeconds/2,visible=points.filter(p=>p.t>=lo&&p.t<=hi),all=visible.flatMap(p=>p.v).filter(Number.isFinite),targetScale=Math.max(20,niceScale(Math.max(1,...all.map(v=>Math.abs(v)))*1.05));forcePlotScale+=((targetScale-forcePlotScale)*(targetScale>forcePlotScale?.18:.025));forcePlotScale=Math.max(20,forcePlotScale);let scale=forcePlotScale,leftPx=58,rightPx=c.width-14,top=34,bottom=c.height-42,mid=(top+bottom)/2,colors=['#e11d48','#22c55e','#2563eb','#f59e0b'],labels=['Fx (External)','Fy (External)','Fz (External)','Grasp Force (Internal)'];x.fillStyle='#111827';x.font='bold 15px system-ui';x.textAlign='center';x.fillText('Readings from CoinFT',c.width/2,18);x.font='11px system-ui';x.textAlign='right';for(let tick=-scale;tick<=scale+1e-9;tick+=scale/2){let py=mid-tick/scale*(bottom-top)/2;x.strokeStyle=Math.abs(tick)<1e-9?'#94a3b8':'#e5e7eb';x.lineWidth=1;x.beginPath();x.moveTo(leftPx,py);x.lineTo(rightPx,py);x.stroke();x.fillStyle='#475467';x.fillText(tick.toFixed(0),leftPx-6,py+4)}for(let i=0;i<=5;i++){let tick=lo+(hi-lo)*i/5,px=leftPx+(rightPx-leftPx)*i/5;x.strokeStyle='#eef0f3';x.beginPath();x.moveTo(px,top);x.lineTo(px,bottom);x.stroke();x.fillStyle='#475467';x.textAlign='center';x.fillText(tick.toFixed(1),px,bottom+16)}x.save();x.beginPath();x.rect(leftPx,top,rightPx-leftPx,bottom-top);x.clip();for(let ch=0;ch<4;ch++){x.beginPath();visible.forEach((p,i)=>{let px=leftPx+(p.t-lo)/(hi-lo)*(rightPx-leftPx),py=mid-p.v[ch]/scale*(bottom-top)/2;i?x.lineTo(px,py):x.moveTo(px,py)});x.strokeStyle=colors[ch];x.lineWidth=2;x.stroke()}x.restore();let cursor=(leftPx+rightPx)/2;x.strokeStyle='#dc2626';x.lineWidth=2;x.setLineDash([6,5]);x.beginPath();x.moveTo(cursor,top);x.lineTo(cursor,bottom);x.stroke();x.setLineDash([]);x.fillStyle='#475467';x.font='12px system-ui';x.textAlign='center';x.fillText('Time [s]',(leftPx+rightPx)/2,c.height-6);x.save();x.translate(12,(top+bottom)/2);x.rotate(-Math.PI/2);x.fillText('Force [N]',0,0);x.restore();x.textAlign='left';labels.forEach((label,i)=>{let lx=leftPx+8+(i%2)*150,ly=top+8+Math.floor(i/2)*17;x.strokeStyle=colors[i];x.lineWidth=3;x.beginPath();x.moveTo(lx,ly);x.lineTo(lx+18,ly);x.stroke();x.fillStyle='#344054';x.fillText(label,lx+24,ly+4)})}
$('datasetRuns').onchange=()=>loadZarrs();$('datasets').onchange=()=>loadEpisodes();$('episodes').onchange=loadEpisode;$('frame').oninput=render;$('play').onclick=()=>{clearInterval(timer);timer=setInterval(()=>{let f=+$('frame').value;if(f>=+$('frame').max)return clearInterval(timer);$('frame').value=f+1;render()},100)};$('pause').onclick=()=>clearInterval(timer);$('globalRgbMode').onclick=()=>{globalMode='rgb';$('globalRgbMode').classList.add('active');$('globalDepthMode').classList.remove('active');render()};$('globalDepthMode').onclick=()=>{globalMode='depth';$('globalDepthMode').classList.add('active');$('globalRgbMode').classList.remove('active');render()};$('gripperRgbMode').onclick=()=>{gripperMode='rgb';$('gripperRgbMode').classList.add('active');$('gripperDepthMode').classList.remove('active');render()};$('gripperDepthMode').onclick=()=>{gripperMode='depth';$('gripperDepthMode').classList.add('active');$('gripperRgbMode').classList.remove('active');render()};
async function refreshCatalog(){let previousRun=runDataset()?.run_dir||'',previousZarr=dataset()?.path||'',previousEpisode=current?.episode?.name||'';let [runs,zarrs]=await Promise.all([get('/api/datasets'),get('/api/catalog')]);runDatasets=runs.datasets;catalog=zarrs.datasets;setOptions('datasetRuns',runDatasets.length?runDatasets.map((run,index)=>({value:String(index),label:`Dataset: ${run.display_name} | ${run.episode_count} episode(s) | ${run.zarr_count} Zarr`})):[{value:'',label:'No Datasets'}]);$('datasetRuns').disabled=!runDatasets.length;if(!runDatasets.length){setOptions('datasets',[{value:'',label:'No Zarr'}]);setOptions('episodes',[{value:'',label:'No Episode'}]);$('datasets').disabled=true;$('episodes').disabled=true;$('deleteZarr').disabled=true;$('datasetMeta').textContent='No Datasets';clearPlayback('No Datasets found');return}let preferredIndex=runDatasets.findIndex(run=>run.run_dir===previousRun);$('datasetRuns').value=String(preferredIndex>=0?preferredIndex:0);await loadZarrs(previousZarr,previousEpisode)}
async function deleteEpisode(){let d=dataset(),e=current?.episode;if(!d||!e)return;let label=e.display_name||e.name,ok=confirm(`Permanently delete ${label}?\n\nThis removes the Zarr Episode and its corresponding iPhone, D435, and CoinFT raw data. Other Episodes in this Dataset are preserved.\n\nThis cannot be undone.`);if(!ok)return;clearInterval(timer);$('deleteEpisode').disabled=true;try{let result=await post('/api/delete-episode',{dataset:d.path,episode:e.name});alert('Deleted '+label+'\n\nRemoved:\n'+(result.removed.join('\n')||'No files found')+'\n\nDataset preserved:\n'+result.dataset_preserved);await refreshCatalog()}catch(err){alert(err);$('deleteEpisode').disabled=false}}
async function deleteZarr(){let run=runDataset(),d=dataset();if(!run)return;let ok=confirm(`Clear all collected data from Dataset "${run.display_name}"?\n\nThis deletes its Zarr, every Episode, all iPhone/D435/CoinFT raw data, and all derived files. The Dataset name and configuration are preserved, and Episode numbering resets to 0.\n\nThis cannot be undone.`);if(!ok)return;clearInterval(timer);$('deleteEpisode').disabled=true;$('deleteZarr').disabled=true;try{let result=await post('/api/delete-zarr',{dataset:d?.path||'',run_dir:run.run_dir});alert('Dataset data cleared.\n\nRemoved:\n'+(result.removed.join('\n')||'No data files found')+'\n\nDataset preserved:\n'+result.dataset_preserved+'\n\nNext Episode: 000000');await refreshCatalog()}catch(err){alert(err);$('deleteEpisode').disabled=false;$('deleteZarr').disabled=false}}
$('deleteEpisode').onclick=deleteEpisode;
$('deleteZarr').onclick=deleteZarr;
refreshCatalog().catch(e=>alert(e));
window.addEventListener('focus',()=>refreshCatalog().catch(e=>console.error(e)));
</script></body></html>'''


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", action="append", type=Path, default=[])
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8900)
    parser.add_argument("--capture-state-url", default="http://127.0.0.1:8765/state")
    args = parser.parse_args()
    roots = args.root or [Path(__file__).resolve().parents[3] / "runs"]
    catalog = ZarrCatalog(roots)

    def assert_deletion_allowed(run_dir: Path) -> None:
        try:
            with urlopen(args.capture_state_url, timeout=1.0) as response:
                payload = json.loads(response.read())
        except Exception as exc:
            raise RuntimeError(f"cannot verify capture is stopped: {exc}") from exc
        blockers = deletion_blockers(payload, run_dir)
        if blockers:
            raise RuntimeError("stop capture before deleting: " + "; ".join(blockers))

    class Handler(BaseHTTPRequestHandler):
        def send_json(self, value: object, status: int = 200):
            body = json.dumps(value, ensure_ascii=False).encode()
            self.send_response(status); self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
        def do_GET(self):
            try:
                parsed=urlparse(self.path); query=parse_qs(parsed.query)
                if parsed.path == "/":
                    body=HTML.encode(); self.send_response(200); self.send_header("Content-Type","text/html; charset=utf-8"); self.send_header("Content-Length",str(len(body))); self.end_headers(); self.wfile.write(body); return
                if parsed.path == "/api/datasets": self.send_json({"datasets":catalog.run_datasets()}); return
                if parsed.path == "/api/catalog": self.send_json({"datasets":catalog.datasets()}); return
                if parsed.path == "/api/episode": self.send_json(catalog.episode(query["dataset"][0],query["episode"][0])); return
                if parsed.path == "/api/series": self.send_json(catalog.series(query["dataset"][0],query["episode"][0],query["field"][0],int(query.get("start",[0])[0]),int(query.get("count",[2000])[0]))); return
                if parsed.path == "/api/frame":
                    data,content_type=catalog.frame(query["dataset"][0],query["episode"][0],query["field"][0],int(query.get("index",[0])[0])); self.send_response(200); self.send_header("Content-Type",content_type); self.send_header("Content-Length",str(len(data))); self.end_headers(); self.wfile.write(data); return
                self.send_json({"error":"not found"},404)
            except Exception as exc: self.send_json({"error":str(exc)},400)
        def do_POST(self):
            try:
                parsed=urlparse(self.path)
                length=int(self.headers.get("Content-Length","0"))
                payload=json.loads(self.rfile.read(length) or b"{}")
                if parsed.path == "/api/delete-episode":
                    dataset_path = catalog.resolve(str(payload.get("dataset", "")))
                    run_dir = catalog.run_for_dataset(dataset_path)
                    assert_deletion_allowed(run_dir)
                    self.send_json(catalog.delete_episode(str(dataset_path), str(payload.get("episode", "")))); return
                if parsed.path == "/api/delete-zarr":
                    run_dir_value = payload.get("run_dir")
                    if run_dir_value:
                        run_dir = catalog.resolve_run(str(run_dir_value))
                    else:
                        dataset_path = catalog.resolve(str(payload.get("dataset", "")))
                        run_dir = catalog.run_for_dataset(dataset_path)
                    assert_deletion_allowed(run_dir)
                    self.send_json(catalog.delete_zarr(str(payload.get("dataset", "")), run_dir=run_dir)); return
                self.send_json({"error":"not found"},404)
            except Exception as exc: self.send_json({"error":str(exc)},400)
        def log_message(self,*args): pass
    server=ThreadingHTTPServer((args.host,args.port),Handler); print(f"Zarr inspector: http://{args.host}:{args.port}",flush=True); server.serve_forever(); return 0

if __name__ == "__main__": raise SystemExit(main())
