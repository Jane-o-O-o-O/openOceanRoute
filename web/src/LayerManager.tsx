import {ArrowUp,ArrowDown,X} from 'lucide-react';
import type {Layer} from './types';
import {layerOpacity,layerVisible} from './layerDisplay';
import './LayerManager.css';

export default function LayerManager({layers,onChange}:{layers:Layer[];onChange:(layers:Layer[])=>void}){
  function patch(id:string,update:(layer:Layer)=>Layer){onChange(layers.map(layer=>layer.id===id?update(layer):layer))}
  function move(index:number,offset:number){const target=index+offset;if(target<0||target>=layers.length)return;const next=[...layers];[next[index],next[target]]=[next[target],next[index]];onChange(next)}
  if(!layers.length)return <div className="empty-layers">暂无 GIS 图层</div>;
  return <div className="gis-layer-manager"><p>上方图层覆盖下方图层</p><ol aria-label="共享 GIS 图层叠放">{[...layers].reverse().map(layer=>{
    const index=layers.findIndex(item=>item.id===layer.id),opacity=layerOpacity(layer);
    return <li key={layer.id} className="gis-layer-control" data-layer-id={layer.id}>
      <div className="layer-row"><input aria-label={`显示 ${layer.name}`} type="checkbox" checked={layerVisible(layer)} onChange={e=>patch(layer.id,item=>({...item,visible:e.target.checked}))}/><span title={layer.name}>{layer.name}</span><div className="layer-stack-buttons"><button type="button" aria-label={`将 ${layer.name} 上移`} title="上移一层" disabled={index===layers.length-1} onClick={()=>move(index,1)}><ArrowUp size={12}/></button><button type="button" aria-label={`将 ${layer.name} 下移`} title="下移一层" disabled={index===0} onClick={()=>move(index,-1)}><ArrowDown size={12}/></button><button type="button" title="移除图层" aria-label={`移除 ${layer.name}`} onClick={()=>onChange(layers.filter(item=>item.id!==layer.id))}><X size={12}/></button></div></div>
      <label className="layer-opacity"><span>不透明度</span><input aria-label={`${layer.name} 不透明度`} type="range" min="0" max="1" step="0.05" value={opacity} onChange={e=>patch(layer.id,item=>({...item,display:{...item.display,opacity:Number(e.target.value)}}))}/><output>{Math.round(opacity*100)}%</output></label>
    </li>
  })}</ol></div>;
}
