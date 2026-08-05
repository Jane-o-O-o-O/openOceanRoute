export async function request<T>(path:string, body?:unknown):Promise<T>{
  const response=await fetch('/api'+path,{method:body===undefined?'GET':'POST',headers:body===undefined?{}:{'Content-Type':'application/json'},body:body===undefined?undefined:JSON.stringify(body)});
  if(!response.ok){let message=await response.text();try{const json=JSON.parse(message);message=typeof json.detail==='string'?json.detail:JSON.stringify(json.detail)}catch{}throw new Error(message||`HTTP ${response.status}`)}
  return response.json();
}
export async function exportProject(format:string,project:unknown){
  const response=await fetch('/api/export/'+format,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(project)});
  if(!response.ok){throw new Error(await response.text())}
  const ext:Record<string,string>={csv:'csv',assembly:'csv',kml:'kml',geojson:'geojson',dxf:'dxf',sld:'svg',report:'html',project:'json'};
  const blob=await response.blob();const url=URL.createObjectURL(blob);const link=document.createElement('a');link.href=url;link.download=`OceanRoute-${format}.${ext[format]||format}`;link.click();setTimeout(()=>URL.revokeObjectURL(url),3000);
}
