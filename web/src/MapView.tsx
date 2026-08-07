import {useEffect,useRef,useState} from 'react';
import L from 'leaflet';
import {Crosshair,Layers,MousePointer2,Plus,Compass} from 'lucide-react';
import type {Project,Point} from './types';

export type PathOverlay={id:string;name:string;segments:number[][][];role?:string};
const noPathOverlays:PathOverlay[]=[];
function tooltipText(value:string){const span=document.createElement('span');span.textContent=value;return span}

function routePositions(points:Point[],curve:string):L.LatLngTuple[]{
  const result:L.LatLngTuple[]=[];let lastLon=points[0]?.longitude||0;
  for(let i=0;i<points.length;i++){
    const p=points[i];let lon=p.longitude;while(lon-lastLon>180)lon-=360;while(lon-lastLon< -180)lon+=360;
    if(i&&curve==='great_circle'){
      const a=points[i-1],b=p,rad=Math.PI/180,lat1=a.latitude*rad,lat2=b.latitude*rad,lon1=lastLon*rad,lon2=lon*rad;
      const d=2*Math.asin(Math.sqrt(Math.sin((lat2-lat1)/2)**2+Math.cos(lat1)*Math.cos(lat2)*Math.sin((lon2-lon1)/2)**2));
      if(d>1e-10&&Math.abs(Math.PI-d)>1e-7)for(let n=1;n<24;n++){const f=n/24,A=Math.sin((1-f)*d)/Math.sin(d),B=Math.sin(f*d)/Math.sin(d),x=A*Math.cos(lat1)*Math.cos(lon1)+B*Math.cos(lat2)*Math.cos(lon2),y=A*Math.cos(lat1)*Math.sin(lon1)+B*Math.cos(lat2)*Math.sin(lon2),z=A*Math.sin(lat1)+B*Math.sin(lat2);let ilon=Math.atan2(y,x)/rad;while(ilon-lastLon>180)ilon-=360;while(ilon-lastLon< -180)ilon+=360;result.push([Math.atan2(z,Math.sqrt(x*x+y*y))/rad,ilon]);}
    }
    result.push([p.latitude,lon]);lastLon=lon;
  }return result;
}

export default function MapView({project,geometry,selected,onSelect,onMove,onAdd,fitVersion,editable=true,allowAdd=true,pathOverlays=noPathOverlays,onPathPick}:{project:Project;geometry?:number[][];selected:string|null;onSelect:(id:string)=>void;onMove:(id:string,lat:number,lon:number)=>void|Promise<boolean>;onAdd:(lat:number,lon:number)=>void;fitVersion:number;editable?:boolean;allowAdd?:boolean;pathOverlays?:PathOverlay[];onPathPick?:(id:string)=>void}){
  const element=useRef<HTMLDivElement>(null),mapRef=useRef<L.Map|null>(null),content=useRef<L.LayerGroup|null>(null),gridRef=useRef<L.LayerGroup|null>(null),initial=useRef(false);
  const callbacks=useRef({onSelect,onMove,onAdd}),addRef=useRef(false);callbacks.current={onSelect,onMove,onAdd};
  const [adding,setAdding]=useState(false),[base,setBase]=useState(false),[cursor,setCursor]=useState('WGS 84 · EPSG:4326');addRef.current=adding&&editable&&allowAdd;
  useEffect(()=>{if(!allowAdd)setAdding(false)},[allowAdd]);
  useEffect(()=>{
    if(!element.current)return;
    const map=L.map(element.current,{zoomControl:false,attributionControl:true,minZoom:2,maxZoom:20,worldCopyJump:false}).setView([22,118],7);mapRef.current=map;
    L.control.zoom({position:'bottomright'}).addTo(map);L.control.scale({imperial:false,position:'bottomleft'}).addTo(map);
    content.current=L.layerGroup().addTo(map);gridRef.current=L.layerGroup().addTo(map);
    const grid=()=>{const group=gridRef.current;if(!group)return;group.clearLayers();const bounds=map.getBounds(),span=bounds.getEast()-bounds.getWest();const step=span>80?20:span>25?5:span>6?1:span>1?.2:span>.15?.05:.01;
      for(let lon=Math.ceil(bounds.getWest()/step)*step;lon<bounds.getEast();lon+=step){L.polyline([[Math.max(-85,bounds.getSouth()),lon],[Math.min(85,bounds.getNorth()),lon]],{color:'#94b5c3',weight:1,opacity:.28,interactive:false}).addTo(group);}
      for(let lat=Math.ceil(bounds.getSouth()/step)*step;lat<bounds.getNorth();lat+=step){L.polyline([[lat,bounds.getWest()],[lat,bounds.getEast()]],{color:'#94b5c3',weight:1,opacity:.28,interactive:false}).addTo(group);}
    };grid();map.on('moveend',grid);
    map.on('mousemove',(e:L.LeafletMouseEvent)=>setCursor(`${e.latlng.lng.toFixed(5)}° E  /  ${e.latlng.lat.toFixed(5)}° N`));
    map.on('click',(e:L.LeafletMouseEvent)=>{if(addRef.current)callbacks.current.onAdd(e.latlng.lat,((e.latlng.lng+180)%360+360)%360-180)});
    const observer=new ResizeObserver(()=>map.invalidateSize());observer.observe(element.current);
    return()=>{observer.disconnect();map.remove();mapRef.current=null;};
  },[]);
  useEffect(()=>{const map=mapRef.current;if(!map)return;const tile=L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png',{attribution:'© OpenStreetMap contributors',maxZoom:19});if(base)tile.addTo(map);return()=>{map.removeLayer(tile)};},[base]);
  useEffect(()=>{
    const map=mapRef.current,group=content.current;if(!map||!group)return;group.clearLayers();
    project.layers.filter(l=>l.visible).forEach(layer=>{try{const geo=L.geoJSON(layer.geojson,{style:{color:layer.kind==='survey_measured'?'#208ca4':layer.kind==='survey_observations'?'#4e78b1':layer.kind==='survey_residuals'?'#c66b73':['cable','routing_original'].includes(layer.kind)?'#d39a42':['restricted','exclusion','hazard','routing_blocked'].includes(layer.kind)?'#bc5865':'#617aa5',weight:2,fillOpacity:.14,dashArray:layer.kind==='survey_residuals'?'3 4':['cable','routing_original'].includes(layer.kind)?'6 5':undefined},pointToLayer:(feature,latlng)=>L.circleMarker(latlng,{radius:5,color:layer.kind==='survey_observations'?(feature.properties?.ambiguity?'#bc9351':feature.properties?.matched?'#4e78b1':'#8c9da7'):'#617aa5'})});geo.bindTooltip(()=>tooltipText(layer.name));geo.addTo(group)}catch{}});
    for(const overlay of pathOverlays){for(const segment of overlay.segments){if(segment.length<2)continue;const positions=segment.map(p=>[p[1],p[0]] as L.LatLngTuple);const path=L.polyline(positions,{color:overlay.role==='alternative'?'#bf9c57':'#708aaf',weight:3,opacity:.65,dashArray:overlay.role==='alternative'?'5 5':undefined,className:'workspace-other-path'});path.bindTooltip(tooltipText(overlay.name+' · '+(overlay.role==='alternative'?'备选路线':'工程路径')));if(onPathPick&&editable)path.on('click',()=>onPathPick(overlay.id));path.addTo(group)}}
    const positions=geometry?.length?geometry.reduce<L.LatLngTuple[]>((acc,p)=>{let lon=p[0];const last=acc.length?acc[acc.length-1][1]:lon;while(lon-last>180)lon-=360;while(lon-last< -180)lon+=360;acc.push([p[1],lon]);return acc},[]):routePositions(project.route.points,project.route.curve);L.polyline(positions,{color:'#158a85',weight:4,opacity:.95}).addTo(group);
    project.route.points.forEach((p,i)=>{let lon=p.longitude;const first=project.route.points[0]?.longitude||0;while(lon-first>180)lon-=360;while(lon-first< -180)lon+=360;
      const marker=L.marker([p.latitude,lon],{draggable:editable&&!(project.route.constraint_state&&p.constraint==='sliding'&&project.route.mode==='fixed'),icon:L.divIcon({className:'route-marker',html:`<span class="${selected===p.id?'selected':''}">${String(i+1).padStart(2,'0')}</span>`,iconSize:[28,28],iconAnchor:[14,14]})});
      marker.bindTooltip(tooltipText(p.label),{direction:'top',offset:[0,-13]});marker.on('click',()=>callbacks.current.onSelect(p.id));marker.on('dragend',async()=>{const pos=marker.getLatLng();if(await callbacks.current.onMove(p.id,pos.lat,((pos.lng+180)%360+360)%360-180)===false)marker.setLatLng([p.latitude,lon])});marker.addTo(group);
    });
    if(!initial.current&&positions.length>1){map.fitBounds(L.latLngBounds(positions),{padding:[55,55]});initial.current=true;}
  },[project,geometry,selected,editable,pathOverlays,onPathPick]);
  useEffect(()=>{const map=mapRef.current;if(map&&project.route.points.length>1)map.fitBounds(L.latLngBounds(routePositions(project.route.points,project.route.curve)),{padding:[60,60]});},[fitVersion]);
  return <div className={`map-shell ${adding?'map-add':''}`}><div className="leaflet-host" ref={element}/><div className="map-tools"><button title="选取与拖动路由点" className={!adding?'selected':''} onClick={()=>setAdding(false)}><MousePointer2 size={17}/></button>{editable&&allowAdd&&<button title="在地图上点击追加路由点" className={adding?'selected':''} onClick={()=>setAdding(!adding)}><Plus size={18}/></button>}<span/><button title="定位完整路由" onClick={()=>{const map=mapRef.current;if(map)map.fitBounds(L.latLngBounds(routePositions(project.route.points,project.route.curve)),{padding:[55,55]})}}><Crosshair size={18}/></button><button title="切换在线 OpenStreetMap 底图" className={base?'selected':''} onClick={()=>setBase(!base)}><Layers size={18}/></button></div><div className="map-corner"><Compass size={25}/><span>N</span></div><div className="map-source"><i/>{base?'OpenStreetMap · 在线底图':'离线经纬网 · 无地形底图'}</div><div className="map-coordinate">{cursor}</div>{adding&&<div className="map-hint">点击地图追加路由点 · 追加点水深待输入</div>}</div>;
}
