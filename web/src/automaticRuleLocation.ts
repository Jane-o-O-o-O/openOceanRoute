import {request} from './api';
import type {RuleErrorLocation} from './types';

/** Read-only display boundary: every vertex is a real WGS84 direct calculation. */
export async function ruleGeographicCircle(location:RuleErrorLocation):Promise<[number,number][]>{
 if(typeof location.radius_m!=='number'||!Number.isFinite(location.radius_m)||location.radius_m<=0)return [];
 const center=location.radius_center||location;if(!Number.isFinite(center.longitude)||!Number.isFinite(center.latitude))throw new Error('邻域圈心缺少真实坐标，未用错误质心替代');
 const boundary=await Promise.all(Array.from({length:32},(_,i)=>request<any>('/tools/geodetic',{config:{from:{longitude:center.longitude,latitude:center.latitude},bearing_deg:i*360/32,distance_m:location.radius_m,curve:'geodesic',segments:1}})));
 return boundary.map((row:any)=>{if(!Number.isFinite(row.to?.longitude)||!Number.isFinite(row.to?.latitude))throw new Error('WGS84 邻域边界计算缺少有限坐标，未显示部分距离圈');return [row.to.longitude,row.to.latitude]});
}
