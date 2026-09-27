import {productRelease} from './version';

export function responseFilename(response:Response,fallback:string):string{
  const disposition=response.headers.get('Content-Disposition')||'';
  const extended=disposition.match(/(?:^|;)\s*filename\*\s*=\s*(?:"([^"]*)"|([^;]*))/i);
  const plain=disposition.match(/(?:^|;)\s*filename\s*=\s*(?:"((?:[^"\\]|\\.)*)"|([^;]*))/i);
  const safe=(name:string)=>!!name&&name!=='.'&&name!=='..'&&!/[\u0000-\u001f\u007f/\\]/.test(name);
  if(extended){const value=(extended[1]??extended[2]).trim(),parts=value.match(/^UTF-8'[^']*'(.*)$/i);if(parts)try{const name=decodeURIComponent(parts[1]);if(safe(name))return name}catch{}}
  if(plain){const name=(plain[1]??plain[2]).trim().replace(/\\(["\\])/g,'$1');if(safe(name))return name}
  return safe(fallback)?fallback:`OceanRoute-${productRelease}-export.bin`;
}
export async function downloadResponse(response:Response,fallback:string){const url=URL.createObjectURL(await response.blob()),link=document.createElement('a');link.href=url;link.download=responseFilename(response,fallback);link.click();setTimeout(()=>URL.revokeObjectURL(url),3000)}
export async function request<T>(path:string, body?:unknown):Promise<T>{
  const response=await fetch('/api'+path,{method:body===undefined?'GET':'POST',headers:body===undefined?{}:{'Content-Type':'application/json'},body:body===undefined?undefined:JSON.stringify(body)});
  if(!response.ok){let message=await response.text();try{const json=JSON.parse(message);message=typeof json.detail==='string'?json.detail:JSON.stringify(json.detail)}catch{}throw new Error(message||`HTTP ${response.status}`)}
  return response.json();
}
export async function exportProject(format:string,project:unknown){
  const response=await fetch('/api/export/'+format,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(project)});
  if(!response.ok){throw new Error(await response.text())}
  const ext:Record<string,string>={csv:'csv',assembly:'csv',kml:'kml',geojson:'geojson',dxf:'dxf',sld:'svg',report:'html',project:'json'};
  await downloadResponse(response,`OceanRoute-${productRelease}-path-${format}.${ext[format]||format}`);
}
