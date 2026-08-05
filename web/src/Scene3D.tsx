import {useEffect,useRef,useState} from 'react';
import * as THREE from 'three';
import {OrbitControls} from 'three/addons/controls/OrbitControls.js';
import type {Frame} from './types';
export default function Scene3D({frame,depth,seabed,showShip=true}:{frame:Frame|null;depth:number;seabed?:{x_m:number;depth_m:number}[];showShip?:boolean}){
  const element=useRef<HTMLDivElement>(null),[error,setError]=useState('');
  useEffect(()=>{
    if(!element.current||!frame)return;const parent=element.current;let renderer:THREE.WebGLRenderer;
    try{renderer=new THREE.WebGLRenderer({antialias:true,alpha:false})}catch{setError('当前环境无法创建 WebGL 视图。下方节点表仍可查看计算结果。');return}
    const scene=new THREE.Scene();scene.background=new THREE.Color('#eff5f7');scene.fog=new THREE.Fog('#eff5f7',depth*7,depth*18);
    const all=[...frame.nodes,frame.ship];const extent=showShip?Math.max(depth,50,...all.flatMap(p=>[Math.abs(p[0]),Math.abs(p[1])]))*1.3:Math.max(30,Math.max(...frame.nodes.map(p=>p[0]))-Math.min(...frame.nodes.map(p=>p[0])),Math.max(...frame.nodes.map(p=>p[2]))-Math.min(...frame.nodes.map(p=>p[2])))*1.6;
    const convert=(p:number[])=>new THREE.Vector3(p[0],p[2],-p[1]);
    const camera=new THREE.PerspectiveCamera(40,parent.clientWidth/Math.max(parent.clientHeight,1),.1,extent*40);const center=showShip?new THREE.Vector3(extent*.12,-depth*.5,0):new THREE.Box3().setFromPoints(frame.nodes.map(convert)).getCenter(new THREE.Vector3());camera.position.copy(center).add(new THREE.Vector3(extent*1.2,extent*.7,extent*1.15));
    const controls=new OrbitControls(camera,renderer.domElement);controls.target.copy(center);controls.enableDamping=true;controls.update();
    renderer.setPixelRatio(Math.min(devicePixelRatio,2));renderer.setSize(parent.clientWidth,parent.clientHeight);parent.appendChild(renderer.domElement);
    scene.add(new THREE.AmbientLight('#ffffff',1.8));const light=new THREE.DirectionalLight('#ffffff',2);light.position.set(1,3,2);scene.add(light);
    const floor=new THREE.GridHelper(extent*5,40,'#7f9ca4','#c3d3d8');floor.position.set(center.x,-Math.max(depth,...(seabed||[]).map(p=>p.depth_m)),center.z);scene.add(floor);if(seabed?.length){const bed=new THREE.Line(new THREE.BufferGeometry().setFromPoints(seabed.map(p=>new THREE.Vector3(p.x_m,-p.depth_m,0))),new THREE.LineBasicMaterial({color:'#77938c'}));scene.add(bed)}
    const sea=new THREE.Mesh(new THREE.PlaneGeometry(extent*5,extent*5),new THREE.MeshBasicMaterial({color:'#60b2cc',transparent:true,opacity:.11,side:THREE.DoubleSide,depthWrite:false}));sea.rotation.x=-Math.PI/2;if(showShip)scene.add(sea);else{sea.geometry.dispose();(sea.material as THREE.Material).dispose();}
    const line=new THREE.Line(new THREE.BufferGeometry().setFromPoints(frame.nodes.map(convert)),new THREE.LineBasicMaterial({color:'#0b8884',linewidth:3}));scene.add(line);
    const sphereGeo=new THREE.SphereGeometry(extent*.005,8,8),nodeMat=new THREE.MeshBasicMaterial({color:'#147f80'});frame.nodes.forEach((p,i)=>{if(i%2===0){const dot=new THREE.Mesh(sphereGeo,nodeMat);dot.position.copy(convert(p));scene.add(dot)}});
    if(showShip){const ship=new THREE.Mesh(new THREE.BoxGeometry(extent*.09,extent*.018,extent*.035),new THREE.MeshStandardMaterial({color:'#203c4f',roughness:.6}));ship.position.copy(convert(frame.ship));scene.add(ship);
    const td=new THREE.Mesh(new THREE.SphereGeometry(extent*.012,16,16),new THREE.MeshBasicMaterial({color:'#dba146'}));if(frame.touchdown){td.position.copy(convert(frame.touchdown));scene.add(td)}}
    for(const b of frame.inline_bodies||[]){if(b.deployed_fraction>0&&b.position?.length===3){const body=new THREE.Mesh(new THREE.SphereGeometry(extent*.009,12,12),new THREE.MeshStandardMaterial({color:'#a7683c',roughness:.7}));body.position.copy(convert(b.position));scene.add(body)}}
    const axes=new THREE.AxesHelper(extent*.25);axes.position.set(center.x-extent*.4,-Math.max(depth,...(seabed||[]).map(p=>p.depth_m)),center.z+extent*.4);scene.add(axes);
    let handle=0;const animate=()=>{handle=requestAnimationFrame(animate);controls.update();renderer.render(scene,camera)};animate();
    const observer=new ResizeObserver(()=>{const w=parent.clientWidth,h=parent.clientHeight;renderer.setSize(w,h);camera.aspect=w/Math.max(h,1);camera.updateProjectionMatrix()});observer.observe(parent);
    return()=>{cancelAnimationFrame(handle);observer.disconnect();controls.dispose();scene.traverse(object=>{if(object instanceof THREE.Mesh||object instanceof THREE.Line){object.geometry.dispose();const mats=Array.isArray(object.material)?object.material:[object.material];mats.forEach(m=>m.dispose())}});renderer.dispose();renderer.domElement.remove();};
  },[frame,depth,seabed,showShip]);
  return <div className="scene-host" ref={element}>{error?<div className="scene-placeholder">{error}</div>:!frame?<div className="scene-placeholder"><div className="wire-cube">3D</div><strong>敷设计算工作空间</strong><span>设置材料、海流与作业参数，运行求解器后查看真实计算节点。</span></div>:null}<div className="scene-legend"><i/>缆形节点 <b/>{showShip?'触地点':'海床剖面'} <span>拖动旋转 · 滚轮缩放 · 右键平移</span></div></div>;
}
