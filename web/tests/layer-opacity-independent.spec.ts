import {test,expect} from '@playwright/test';
import {apiURL} from './test-environment';
import {artifactPath} from './artifact-path';

const layerId='independent-overlapping-lines';
const layerName='独立重合线对照';
async function ready(page:any){await expect(page.locator('.calculation-status')).toHaveText('工程计算已更新',{timeout:20000})}

test('one whole-layer opacity composites coincident strokes in both real maps and preserves omitted visibility and source data',async({page,request})=>{
  const errors:string[]=[];page.on('pageerror',error=>errors.push(error.message));
  const sampleResponse=await request.get(apiURL+'/sample');expect(sampleResponse.status()).toBe(200);
  const project=await sampleResponse.json();project.name='Independent layer composite '+Date.now();
  // This deliberately synthetic display fixture has two identical features in
  // one layer. It is neither a chart nor measured survey data. Omitted visible
  // exercises the open schema's actual default instead of adding a UI default.
  const coordinates=[[118,22],[118.05,22.05]];
  const fixture={id:layerId,name:layerName,kind:'reference',display:{opacity:.5},
    source:{format:'explicit-synthetic-overlap-review',description:'two coincident lines; display test only',nested:{preserve:['coordinates','source',0,null]}},
    geojson:{type:'FeatureCollection',features:[0,1].map(index=>({type:'Feature',properties:{record:index+1},geometry:{type:'LineString',coordinates}}))}};
  project.layers=[fixture];
  // Only sample admission is intercepted; migration, analysis, projection,
  // persistence and reopening below all run the actual backend.
  await page.route('**/api/sample',route=>route.fulfill({json:project}));
  const migration=page.waitForResponse(response=>response.url().endsWith('/api/workspace/migrate'));
  await page.goto('/');const migrationResponse=await migration;expect(migrationResponse.status()).toBe(200);
  const initial=(await migrationResponse.json()).workspace;await ready(page);
  expect(Object.prototype.hasOwnProperty.call(initial.layers[0],'visible')).toBe(false);
  await expect(page.getByRole('checkbox',{name:'显示 '+layerName,exact:true})).toBeChecked();
  await page.getByRole('button',{name:'定位可见 GIS 图层',exact:true}).click();

  const geographicGroup=page.locator('.leaflet-host g[data-gis-layer-group-id="'+layerId+'"]');
  const geographicPaths=page.locator('.leaflet-host path[data-gis-layer-id="'+layerId+'"]');
  await expect(geographicGroup).toHaveCount(1);await expect(geographicGroup).toHaveAttribute('opacity','0.5');
  await expect(geographicPaths).toHaveCount(2);
  await expect.poll(()=>geographicPaths.first().evaluate(node=>{const box=(node as SVGGraphicsElement).getBBox();return Math.min(box.width,box.height)})).toBeGreaterThan(0);
  const geographic=await geographicPaths.evaluateAll(nodes=>nodes.map(node=>({
    d:node.getAttribute('d'),strokeOpacity:node.getAttribute('stroke-opacity'),
    computedStrokeOpacity:getComputedStyle(node).strokeOpacity,
    parentLayer:node.parentElement?.getAttribute('data-gis-layer-group-id'),
    parentOpacity:node.parentElement?.getAttribute('opacity'),parentChildren:node.parentElement?.children.length,
  })));
  expect(geographic[0].d).toBeTruthy();expect(geographic[0].d).toBe(geographic[1].d);
  for(const stroke of geographic){expect(stroke.strokeOpacity).toBe('1');expect(Number(stroke.computedStrokeOpacity)).toBe(1);expect(stroke.parentLayer).toBe(layerId);expect(stroke.parentOpacity).toBe('0.5');expect(stroke.parentChildren).toBe(2)}
  // Both full strokes must be inside one group: two independent alpha=.5
  // strokes would compose to .75 where they overlap, not the requested .5.
  expect(await geographicPaths.evaluateAll(nodes=>nodes[0].parentElement===nodes[1].parentElement)).toBe(true);

  // A real display edit/redraw must remove the old composite group, preserve
  // both Leaflet paths and leave the original missing visible field missing.
  const slider=page.getByRole('slider',{name:layerName+' 不透明度',exact:true});
  await slider.focus();await slider.press('ArrowRight');await ready(page);await expect(geographicGroup).toHaveAttribute('opacity','0.55');
  await slider.press('ArrowLeft');await ready(page);await expect(geographicGroup).toHaveCount(1);await expect(geographicGroup).toHaveAttribute('opacity','0.5');await expect(geographicPaths).toHaveCount(2);

  await page.getByRole('button',{name:'投影视图',exact:true}).click();
  await page.getByLabel('地图显示投影 CRS',{exact:true}).fill('EPSG:32650');
  const projection=page.waitForResponse(response=>response.url().endsWith('/api/maps/project'));
  await page.getByRole('button',{name:'计算真实投影视图',exact:true}).click();const projectionResponse=await projection;expect(projectionResponse.status()).toBe(200);
  const projectedResult=await projectionResponse.json();expect(projectedResult.can_display).toBe(true);
  const projectedLayer=projectedResult.layers.find((layer:any)=>layer.id===layerId);expect(projectedLayer.visible).toBe(true);
  expect(projectedLayer.geojson.features[0].geometry.coordinates).toEqual(projectedLayer.geojson.features[1].geometry.coordinates);
  const projectedGroup=page.locator('.projected-gis-layer[data-layer-id="'+layerId+'"]');
  await expect(page.getByRole('img',{name:'实际投影路线与 GIS 工程平面',exact:true})).toBeVisible();
  await expect(projectedGroup).toHaveAttribute('opacity','0.5');await expect(projectedGroup.locator('path')).toHaveCount(2);
  const projected=await projectedGroup.locator('path').evaluateAll(nodes=>nodes.map(node=>({
    d:node.getAttribute('d'),strokeOpacity:getComputedStyle(node).strokeOpacity,
    parentLayer:node.parentElement?.getAttribute('data-layer-id'),parentOpacity:node.parentElement?.getAttribute('opacity'),
  })));
  expect(projected[0].d).toBeTruthy();expect(projected[0].d).toBe(projected[1].d);
  for(const stroke of projected){expect(Number(stroke.strokeOpacity)).toBe(1);expect(stroke.parentLayer).toBe(layerId);expect(stroke.parentOpacity).toBe('0.5')}
  expect(await projectedGroup.locator('path').evaluateAll(nodes=>nodes[0].parentElement===nodes[1].parentElement)).toBe(true);

  await page.getByRole('button',{name:'保存',exact:true}).click();await expect(page.getByText('工程已保存 · 修订 1',{exact:true})).toBeVisible();await ready(page);
  const savedResponse=await request.get(apiURL+'/workspaces/'+initial.id);expect(savedResponse.status()).toBe(200);const saved=await savedResponse.json();
  expect(saved.layers).toEqual([fixture]);expect(Object.prototype.hasOwnProperty.call(saved.layers[0],'visible')).toBe(false);
  for(const key of ['paths','assemblies','associations','cable_types','terrain_sources','active_path_id'])expect(saved[key]).toEqual(initial[key]);
  // Remount Leaflet and then reopen the real stored workspace; no stale or
  // detached composite groups may survive either operation.
  await page.getByRole('button',{name:'经纬地图',exact:true}).click();await expect(geographicGroup).toHaveCount(1);await expect(geographicPaths).toHaveCount(2);
  await page.getByRole('button',{name:'打开',exact:true}).click();await page.locator('.project-list button').filter({has:page.getByText(project.name,{exact:true})}).click();await ready(page);
  await expect(page.getByRole('checkbox',{name:'显示 '+layerName,exact:true})).toBeChecked();await expect(geographicGroup).toHaveCount(1);await expect(geographicGroup).toHaveAttribute('opacity','0.5');await expect(geographicPaths).toHaveCount(2);
  expect((await(await request.get(apiURL+'/workspaces/'+initial.id)).json()).layers).toEqual([fixture]);
  const close=page.getByRole('button',{name:'关闭提示',exact:true});if(await close.isVisible())await close.click();
  await page.screenshot({path:artifactPath('actual-whole-layer-opacity-overlap-review.png'),fullPage:true});expect(errors).toEqual([]);
});
