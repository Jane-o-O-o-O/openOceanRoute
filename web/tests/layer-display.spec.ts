import {test,expect} from '@playwright/test';
import {apiURL} from './test-environment';
import {artifactPath} from './artifact-path';

async function ready(page:any){await expect(page.locator('.calculation-status')).toHaveText('工程计算已更新',{timeout:20000})}
async function seed(page:any,request:any){
 const p=await(await request.get(apiURL+'/sample')).json();p.name='GIS Display '+Date.now();
 const polygon=(west:number,south:number)=>({type:'FeatureCollection',features:[{type:'Feature',properties:{label:'explicit display-only fixture'},geometry:{type:'Polygon',coordinates:[[[west,south],[west+.06,south],[west+.06,south+.06],[west,south+.06],[west,south]],[[west+.01,south+.01],[west+.02,south+.01],[west+.02,south+.02],[west+.01,south+.02],[west+.01,south+.01]]]}}]});
 p.layers=[{id:'display-lower',name:'底部范围',kind:'reference',visible:true,display:{opacity:.9},source:{format:'explicit-synthetic-fixture',nested:{keep:['a',null,3]}},geojson:polygon(118.02,22.02)},{id:'display-upper',name:'上层标记',kind:'reference',visible:true,display:{opacity:.4},source:{format:'explicit-synthetic-fixture',nested:{keep:['b',null,4]}},geojson:polygon(118.025,22.025)}];
 await page.route('**/api/sample',async r=>r.fulfill({json:p}));
 const migration=page.waitForResponse(r=>r.url().endsWith('/api/workspace/migrate'));
 await page.goto('/');const initial=(await(await migration).json()).workspace;await ready(page);return {p,initial};
}
async function slider(page:any,name:string,steps:number){const field=page.getByRole('slider',{name:name+' 不透明度',exact:true});await field.focus();await field.press('Home');for(let i=0;i<steps;i++)await field.press('ArrowRight');await ready(page)}
async function closeNotice(page:any){if(await page.getByRole('button',{name:'关闭提示',exact:true}).isVisible())await page.getByRole('button',{name:'关闭提示',exact:true}).click()}
async function projectedShape(page:any){return page.locator('.projected-gis-layer[data-layer-id="display-lower"] path').evaluate((node:SVGPathElement)=>{const point=document.querySelector('.projected-route-point')!,circle=point.querySelector('circle')!,x=Number(circle.getAttribute('cx')),y=Number(circle.getAttribute('cy')),numbers=(node.getAttribute('d')||'').match(/-?\d+(?:\.\d+)?(?:e[+-]?\d+)?/gi)!.map(Number);return {point:[point.getAttribute('data-x'),point.getAttribute('data-y')],relative:numbers.map((value,i)=>value-(i%2?y:x))}})}

test('actual geographic layer opacity and stacking persist without changing geometry, stock or provenance',async({page,request})=>{
 const errors:string[]=[];page.on('pageerror',e=>errors.push(e.message));const {p,initial}=await seed(page,request);
 const paths=page.locator('path[data-gis-layer-id]');await expect(paths).toHaveCount(2);
 expect(await paths.evaluateAll(nodes=>nodes.map(n=>n.getAttribute('data-gis-layer-id')))).toEqual(['display-lower','display-upper']);
 await expect(page.locator('g[data-gis-layer-group-id="display-upper"]')).toHaveAttribute('opacity','0.4');
 await page.getByRole('button',{name:'将 底部范围 上移',exact:true}).click();await ready(page);
 expect(await paths.evaluateAll(nodes=>nodes.map(n=>n.getAttribute('data-gis-layer-id')))).toEqual(['display-upper','display-lower']);
 await slider(page,'上层标记',5);await expect(page.locator('g[data-gis-layer-group-id="display-upper"]')).toHaveAttribute('opacity','0.25');
 await expect(page.locator('path[data-gis-layer-id="display-upper"]')).toHaveAttribute('stroke-opacity','1');
 expect(Number(await page.locator('path[data-gis-layer-id="display-upper"]').getAttribute('fill-opacity'))).toBeCloseTo(.14,10);
 await page.getByRole('button',{name:'定位可见 GIS 图层',exact:true}).click();
 await page.getByRole('button',{name:'保存',exact:true}).click();await expect(page.getByText('工程已保存 · 修订 1',{exact:true})).toBeVisible();await ready(page);
 const saved=await(await request.get(apiURL+'/workspaces/'+initial.id)).json();expect(saved.layers.map((l:any)=>l.id)).toEqual(['display-upper','display-lower']);
 expect(saved.layers[0].display.opacity).toBe(.25);for(const layer of p.layers){const actual=saved.layers.find((l:any)=>l.id===layer.id);expect(actual.geojson).toEqual(layer.geojson);expect(actual.source).toEqual(layer.source)}
 for(const key of ['paths','assemblies','associations','cable_types','terrain_sources'])expect(saved[key]).toEqual(initial[key]);
 await page.getByRole('button',{name:'打开',exact:true}).click();await page.locator('.project-list button').filter({has:page.getByText(p.name,{exact:true})}).click();await ready(page);
 await expect(page.getByRole('slider',{name:'上层标记 不透明度',exact:true})).toHaveValue('0.25');await expect(page.locator('g[data-gis-layer-group-id="display-upper"]')).toHaveAttribute('opacity','0.25');
 await page.getByRole('checkbox',{name:'显示 上层标记',exact:true}).uncheck();await ready(page);await expect(page.locator('path[data-gis-layer-id="display-upper"]')).toHaveCount(0);
 await page.getByRole('button',{name:'撤销 ⌘Z'}).click();await ready(page);await expect(page.locator('path[data-gis-layer-id="display-upper"]')).toHaveCount(1);
 await closeNotice(page);await page.screenshot({path:artifactPath('actual-gis-stack-opacity-persisted.png'),fullPage:true});expect(errors).toEqual([]);
});

test('projected GIS opacity and layer order update the actual SVG without discarding valid projected geometry',async({page,request})=>{
 await seed(page,request);await page.getByRole('button',{name:'投影视图',exact:true}).click();await page.getByLabel('地图显示投影 CRS',{exact:true}).fill('EPSG:32650');
 const calculation=page.waitForResponse(r=>r.url().endsWith('/api/maps/project'));await page.getByRole('button',{name:'计算真实投影视图',exact:true}).click();const actual=await(await calculation).json();expect(actual.can_display).toBe(true);
 const plane=page.getByRole('img',{name:'实际投影路线与 GIS 工程平面',exact:true});await expect(plane).toBeVisible();
 const before=await projectedShape(page);
 let extraRequests=0;page.on('request',r=>{if(r.url().endsWith('/api/maps/project'))extraRequests++});
 await page.getByRole('button',{name:'将 底部范围 上移',exact:true}).click();await ready(page);
 await slider(page,'底部范围',6);await expect(plane).toBeVisible();await expect(page.locator('.projected-map-stale')).toHaveCount(0);
 expect(await page.locator('.projected-gis-layer').evaluateAll(nodes=>nodes.map(n=>n.getAttribute('data-layer-id')))).toEqual(['display-upper','display-lower']);
 await expect(page.locator('.projected-gis-layer[data-layer-id="display-lower"]')).toHaveAttribute('opacity','0.3');
 // Screen origin moves with the actual viewport height; compare the shape
 // relative to the same real route point, and its unchanged native X/Y.
 const after=await projectedShape(page);expect(after.point).toEqual(before.point);expect(after.relative).toHaveLength(before.relative.length);after.relative.forEach((value:number,i:number)=>expect(value).toBeCloseTo(before.relative[i],8));expect(extraRequests).toBe(0);
 await expect(page.locator('.projected-gis-layer[data-layer-id="display-lower"] path')).toHaveAttribute('fill-rule','evenodd');
 await page.getByRole('button',{name:'移除 上层标记',exact:true}).click();await ready(page);await expect(page.getByRole('checkbox',{name:'显示 上层标记',exact:true})).toHaveCount(0);
 await page.getByRole('button',{name:'撤销 ⌘Z'}).click();await ready(page);await expect(page.getByRole('checkbox',{name:'显示 上层标记',exact:true})).toBeChecked();
 await closeNotice(page);await page.screenshot({path:artifactPath('actual-projected-gis-stack-opacity.png'),fullPage:true});
});
