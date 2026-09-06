/** Explicit synthetic input only. These positions are an independent force-balance
 * seed, not a returned solver result, field measurement, or saved checkpoint. */
export function heterogeneousInitialExample(pointWetWeight=-50){
  const rest=[12,2,3,1,4,2,3],origin=4,top=origin+rest.reduce((a,b)=>a+b,0);
  const coordinates=[top];for(const length of rest)coordinates.push(coordinates.at(-1)!-length);
  const aLength=rest.map((_,i)=>Math.max(0,Math.min(coordinates[i],10.2)-Math.max(coordinates[i+1],0)));
  const compliance=rest.map((length,i)=>aLength[i]/10000+(length-aLength[i])/24000);
  const segmentWet=rest.map((length,i)=>4*aLength[i]+7*(length-aLength[i]));
  const nodeWet=[segmentWet[0]/2,...segmentWet.slice(1).map((weight,i)=>(segmentWet[i]+weight)/2),segmentWet.at(-1)!/2];
  const pointMaterial=15.3;
  for(let i=0;i<rest.length;i++)if(coordinates[i+1]<=pointMaterial&&pointMaterial<=coordinates[i]){
    nodeWet[i]+=pointWetWeight*(pointMaterial-coordinates[i+1])/rest[i];
    nodeWet[i+1]+=pointWetWeight*(coordinates[i]-pointMaterial)/rest[i];break;
  }
  const vertical=rest.map(()=>0);vertical[rest.length-1]=15;
  for(let i=rest.length-1;i>0;i--)vertical[i-1]=vertical[i]+nodeWet[i];
  const positions:number[][]=[[0,0,-2]];
  rest.forEach((length,i)=>{const tension=Math.hypot(150,vertical[i]),factor=length*(1+tension/(length/compliance[i]))/tension,previous=positions.at(-1)!;positions.push([previous[0]-150*factor,0,previous[2]-vertical[i]*factor])});
  return {
    depth_m:100,wet_weight_n_m:4,bottom_tension_n:150,nodes:8,ship_speed_m_s:0,payout_m_s:0,heading_deg:90,
    duration_s:.08,dt_s:.02,internal_dt_s:.002,solver_iterations:32,current_x_m_s:0,current_y_m_s:0,
    diameter_m:.02,drag_coefficient:1.2,water_density_kg_m3:1025,ea_n:10000,ei_n_m2:0,mass_kg_m:1.2,
    added_mass_coefficient:1,damping_ratio:.03,seabed_friction:0,heave_amplitude_m:0,heave_period_s:8,
    initial_suspended_material_m:origin,min_bend_radius_m:0,max_tension_n:1e12,
    material_segments:[{id:'A',start_m:0,end_m:10.2,wet_weight_n_m:4,ea_n:10000,ei_n_m2:0,mass_kg_m:1.2,diameter_m:.02,drag_coefficient:1.2},
      {id:'B',start_m:10.2,end_m:100,wet_weight_n_m:7,ea_n:24000,ei_n_m2:0,mass_kg_m:1.8,diameter_m:.03,drag_coefficient:1.2}],
    inline_bodies:[{id:pointWetWeight<0?'synthetic-point-buoy':'synthetic-point-weight',material_m:pointMaterial,length_m:0,mass_kg:10,wet_weight_n:pointWetWeight,drag_area_m2:.1,drag_coefficient:1.2}],
    seabed_grid:{schema:'oceanroute.bathymetry.v1',x_m:[-200,0,200],y_m:[-100,0,100],z_m:[[-100,-100,-100],[-100,-100,-100],[-100,-100,-100]],
      source:{name:'明确合成双缆型 / 零长度点负载离床初态，非现场数据',horizontal_crs:'LOCAL_CARTESIAN_METRES',origin_projected_m:[0,0],vertical_datum:'合成模型海面 z=0'}},
    initial_equilibrium:{schema:'oceanroute.dynamic.initial-equilibrium.v1',vessel_position_m:positions[0],anchor_position_m:positions.at(-1),rest_lengths_m:rest,initial_positions_m:positions}
  };
}
