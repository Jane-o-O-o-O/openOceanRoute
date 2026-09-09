import {apiURL} from './test-environment';
import {artifactPath} from './artifact-path';
import {test,expect} from '@playwright/test';

async function ready(page:any){await expect(page.locator('.calculation-status')).toHaveText('工程计算已更新',{timeout:20000})}
test('planning results agree with the backend and fixed cable survives a coordinate edit',async({page,request})=>{
  await page.goto('/');await ready(page);
  const sample=await (await request.get(apiURL+'/sample')).json();
  const result=await (await request.post(apiURL+'/analyze',{data:sample})).json();
  await expect(page.locator('.metric').nth(0).locator('strong')).toHaveText((result.summary.surface_length_m/1000).toLocaleString('zh-CN',{minimumFractionDigits:3,maximumFractionDigits:3}));
  await page.getByRole('combobox',{name:'缆长模式',exact:true}).selectOption('fixed');await ready(page);
  const cable=await page.locator('.metric').nth(2).locator('strong').innerText();
  await page.getByLabel('经度 / °E',{exact:true}).fill(String(sample.route.points[0].longitude+.02));
  await page.getByLabel('经度 / °E',{exact:true}).blur();await ready(page);
  await expect(page.locator('.metric').nth(2).locator('strong')).toHaveText(cable);
  await expect(page.locator('.metric').nth(1).locator('strong')).toHaveText('—');
  await page.getByRole('button',{name:'撤销 ⌘Z'}).click();await ready(page);
  await expect(page.getByLabel('经度 / °E',{exact:true})).toHaveValue(String(sample.route.points[0].longitude));
  await page.screenshot({path:artifactPath('planning-verified.png'),fullPage:true});
});

test('new project saves twice, reopens and restores a revision',async({page,request})=>{
  await page.goto('/');await ready(page);
  // Delay only delivery of the real migrate response to exercise the loading boundary.
  let release!:()=>void,arrived!:(workspace:any)=>void;const held=new Promise<void>(r=>release=r),nativeReady=new Promise<any>(r=>arrived=r);
  await page.route('**/api/workspace/migrate',async route=>{const actual=await route.fetch();expect(actual.status(),await actual.text()).toBe(200);arrived((await actual.json()).workspace);await held;await route.fulfill({response:actual})});
  const createdResponse=page.waitForResponse(r=>r.url().endsWith('/api/workspace/migrate')&&r.request().method()==='POST');await page.getByRole('button',{name:'新建',exact:true}).click();const created=await nativeReady;
  await expect(page.locator('.document-loading-overlay')).toBeVisible();await expect(page.locator('.calculation-status')).toHaveText('正在载入工作区');expect(await page.locator('.application').evaluate((el:HTMLElement)=>el.closest('[inert]')!==null)).toBe(true);
  release();expect((await createdResponse).status()).toBe(200);await expect(page.locator('.document-loading-overlay')).not.toBeVisible();await ready(page);await page.unroute('**/api/workspace/migrate');await expect(page.locator('.project-title strong')).toHaveText('未命名海缆工程');
  await page.getByRole('button',{name:'工程设置',exact:true}).click();await expect(page.getByLabel('工程编号',{exact:true})).toHaveValue(created.id);
  let releaseRename!:()=>void,arrivedRename!:()=>void;const renameHeld=new Promise<void>(r=>releaseRename=r),renameNativeReady=new Promise<void>(r=>arrivedRename=r);await page.route('**/api/workspace/action',async route=>{if(route.request().postDataJSON()?.config?.action!=='update_metadata'){await route.continue();return}const actual=await route.fetch();expect(actual.status(),await actual.text()).toBe(200);arrivedRename();await renameHeld;await route.fulfill({response:actual})});
  const name='UI regression '+Date.now(),renamedResponse=page.waitForResponse(r=>r.url().endsWith('/api/workspace/action')&&r.request().postDataJSON()?.config?.action==='update_metadata'&&r.request().postDataJSON()?.config?.name===name);await page.getByLabel('工程名称',{exact:true}).fill(name);await page.getByLabel('工程名称',{exact:true}).blur();await renameNativeReady;await expect(page.getByRole('button',{name:'打开',exact:true})).toBeDisabled();await expect(page.getByRole('button',{name:'修订历史',exact:true})).toBeDisabled();releaseRename();const renamed=await renamedResponse;expect(renamed.status()).toBe(200);const renamedWorkspace=(await renamed.json()).workspace;expect(renamedWorkspace.id).toBe(created.id);expect(renamedWorkspace.name).toBe(name);await expect(page.locator('.project-title strong')).toHaveText(name);await ready(page);await page.unroute('**/api/workspace/action');
  await page.getByRole('button',{name:'保存',exact:true}).click();await expect(page.getByText('工程已保存 · 修订 1',{exact:true})).toBeVisible();await ready(page);
  await page.getByRole('button',{name:'保存',exact:true}).click();await expect(page.getByText('工程已保存 · 修订 2',{exact:true})).toBeVisible();await ready(page);
  const projects=await (await request.get(apiURL+'/workspaces')).json();const saved=projects.find((p:any)=>p.name===name);expect(saved).toBeDefined();expect(saved.id).toBe(created.id);expect(saved.revision).toBe(2);
  await page.getByRole('button',{name:'打开',exact:true}).click();const openedResponse=page.waitForResponse(r=>r.url().endsWith('/api/workspaces/'+saved.id)&&r.request().method()==='GET');await page.getByRole('button').filter({has:page.getByText(name,{exact:true})}).click();expect((await openedResponse).status()).toBe(200);await expect(page.locator('.document-loading-overlay')).not.toBeVisible();await expect(page.getByRole('dialog')).not.toBeVisible();await ready(page);
  await page.getByRole('button',{name:'修订历史',exact:true}).click();const restoredResponse=page.waitForResponse(r=>r.url().endsWith('/api/workspaces/'+saved.id+'/restore/1')&&r.request().method()==='POST');await page.getByRole('button').filter({has:page.getByText('修订 1 · '+name,{exact:true})}).click();const response=await restoredResponse;expect(response.status()).toBe(200);const restoredData=await response.json();expect(restoredData.saved_revision).toBe(3);await expect(page.locator('.document-loading-overlay')).not.toBeVisible();await expect(page.getByRole('dialog')).not.toBeVisible();await ready(page);
  const restored=await (await request.get(apiURL+'/workspaces/'+saved.id)).json();expect(restored.saved_revision).toBe(3);
});

test('CSV replacement, signed profile import, GeoJSON and every export work',async({page})=>{
  const errors:string[]=[];page.on('pageerror',err=>errors.push(err.message));
  await page.goto('/');await ready(page);await page.getByRole('button',{name:'导入数据',exact:true}).click();
  await page.locator('.import-text').fill('label,longitude,latitude,depth_m\nAlpha,118,22,30\nBeta,118.01,22.01,100');
  await page.getByRole('button',{name:'解析预览',exact:true}).click();await expect(page.getByRole('button',{name:'应用 RPL 导入',exact:true})).toBeEnabled();await page.getByRole('button',{name:'应用 RPL 导入',exact:true}).click();await expect(page.getByRole('dialog')).not.toBeVisible();await ready(page);
  await page.getByRole('button',{name:'路由位置表',exact:true}).click();await expect(page.locator('.rpl-table tbody tr')).toHaveCount(2);
  await page.getByRole('button',{name:'导入数据',exact:true}).click();await page.getByRole('button',{name:'测深剖面',exact:true}).click();
  await page.locator('.import-text').fill('kp_m,depth_m\n0,30\n1000,60\n3000,100');await page.getByRole('button',{name:'导入数据',exact:true}).last().click();await expect(page.getByRole('dialog')).not.toBeVisible();await ready(page);
  await expect(page.locator('.metric').nth(1).locator('strong')).not.toHaveText('—');
  await page.getByRole('button',{name:'导入数据',exact:true}).click();await page.getByRole('button',{name:'GIS 图层',exact:true}).click();
  await page.locator('.import-text').fill(JSON.stringify({type:'FeatureCollection',features:[{type:'Feature',properties:{name:'crossing'},geometry:{type:'LineString',coordinates:[[118,22.01],[118.01,22]]}}]}));
  await page.getByRole('button',{name:'导入数据',exact:true}).last().click();await expect(page.getByRole('dialog')).not.toBeVisible();await ready(page);await expect(page.locator('.layer-row')).toHaveCount(3);
  await page.getByRole('button',{name:'导出成果',exact:false}).click();
  for(const label of ['RPL 表格 · CSV','地理路线 · KML','路线 · GeoJSON','CAD 图纸 · DXF','电缆单线图 · SVG','工程报告 · HTML','完整工程 · JSON']){
    const download=page.waitForEvent('download');await page.getByRole('button').filter({has:page.getByText(label,{exact:true})}).click();const file=await download;expect(await file.failure()).toBeNull();expect(file.suggestedFilename()).toContain('OceanRoute');
  }expect(errors).toEqual([]);
});

test('all physics solvers render real nodes and dynamic frames play',async({page})=>{
  const errors:string[]=[];page.on('pageerror',err=>errors.push(err.message));
  await page.goto('/');await ready(page);await page.getByRole('button',{name:'敷设仿真',exact:true}).click();
  for(const kind of ['解析悬链线','三维稳态','悬空段','动态敷设']){
    await page.locator('.solver-tabs button').filter({has:page.getByText(kind,{exact:true})}).click();
    if(kind==='动态敷设'){await page.getByLabel('模拟时长 / s',{exact:true}).fill('2');await page.getByLabel('模拟时长 / s',{exact:true}).blur();await page.getByLabel('离散节点数',{exact:true}).fill('10');await page.getByLabel('离散节点数',{exact:true}).blur();}
    await page.getByRole('button',{name:'运行计算',exact:true}).click();await expect(page.locator('.sim-results > .summary-fields')).toBeVisible({timeout:45000});await expect(page.locator('.scene-host canvas')).toBeVisible();await page.locator('.nodes-table summary').click();expect(await page.locator('.nodes-table tbody tr').count()).toBeGreaterThan(2);
    await page.locator('.sim-main').evaluate((el:HTMLElement)=>el.scrollTop=0);await page.locator('.sim-settings').evaluate((el:HTMLElement)=>el.scrollTop=0);await page.screenshot({path:artifactPath(`${kind}.png`),fullPage:true});
  }await expect(page.locator('.playback input')).toHaveAttribute('max',/[1-9][0-9]*/);await page.getByRole('button',{name:'播放计算帧',exact:true}).click();await expect(page.locator('.sim-overlay strong')).not.toHaveText('t = 0.00 s');expect(errors).toEqual([]);
});

test('mobile layout contains its tables and navigation',async({page})=>{
  await page.setViewportSize({width:390,height:844});await page.goto('/');await ready(page);
  expect(await page.evaluate(()=>document.body.scrollWidth)).toBeLessThanOrEqual(390);
  await page.screenshot({path:artifactPath('mobile.png'),fullPage:true});
});
