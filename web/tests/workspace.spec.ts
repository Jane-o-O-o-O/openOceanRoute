import {test,expect} from '@playwright/test';

async function ready(page:any){await expect(page.locator('.calculation-status')).toHaveText('工程计算已更新',{timeout:20000})}
test('planning results agree with the backend and fixed cable survives a coordinate edit',async({page,request})=>{
  await page.goto('/');await ready(page);
  const sample=await (await request.get('http://127.0.0.1:8765/api/sample')).json();
  const result=await (await request.post('http://127.0.0.1:8765/api/analyze',{data:sample})).json();
  await expect(page.locator('.metric').nth(0).locator('strong')).toHaveText((result.summary.surface_length_m/1000).toLocaleString('zh-CN',{minimumFractionDigits:3,maximumFractionDigits:3}));
  await page.getByRole('combobox',{name:'缆长模式',exact:true}).selectOption('fixed');await ready(page);
  const cable=await page.locator('.metric').nth(2).locator('strong').innerText();
  await page.getByLabel('经度 / °E',{exact:true}).fill(String(sample.route.points[0].longitude+.02));
  await page.getByLabel('经度 / °E',{exact:true}).blur();await ready(page);
  await expect(page.locator('.metric').nth(2).locator('strong')).toHaveText(cable);
  await expect(page.locator('.metric').nth(1).locator('strong')).toHaveText('—');
  await page.getByRole('button',{name:'撤销 ⌘Z'}).click();await ready(page);
  await expect(page.getByLabel('经度 / °E',{exact:true})).toHaveValue(String(sample.route.points[0].longitude));
  await page.screenshot({path:'artifacts/planning-verified.png',fullPage:true});
});

test('new project saves twice, reopens and restores a revision',async({page,request})=>{
  await page.goto('/');await ready(page);await page.getByRole('button',{name:'新建',exact:true}).click();await ready(page);
  await page.getByRole('button',{name:'工程设置',exact:true}).click();
  const name='UI regression '+Date.now();await page.getByLabel('工程名称',{exact:true}).fill(name);await page.getByLabel('工程名称',{exact:true}).blur();await ready(page);
  await page.getByRole('button',{name:'保存',exact:true}).click();await expect(page.getByText('工程已保存 · 修订 1',{exact:true})).toBeVisible();await ready(page);
  await page.getByRole('button',{name:'保存',exact:true}).click();await expect(page.getByText('工程已保存 · 修订 2',{exact:true})).toBeVisible();await ready(page);
  const projects=await (await request.get('http://127.0.0.1:8765/api/projects')).json();const saved=projects.find((p:any)=>p.name===name);expect(saved.revision).toBe(2);
  await page.getByRole('button',{name:'打开',exact:true}).click();await page.getByRole('button').filter({has:page.getByText(name,{exact:true})}).click();await ready(page);
  await page.getByRole('button',{name:'修订历史',exact:true}).click();await page.getByRole('button').filter({has:page.getByText('修订 1 · '+name,{exact:true})}).click();await ready(page);
  const restored=await (await request.get('http://127.0.0.1:8765/api/projects/'+saved.id)).json();expect(restored.saved_revision).toBe(3);
});

test('CSV replacement, signed profile import, GeoJSON and every export work',async({page})=>{
  const errors:string[]=[];page.on('pageerror',err=>errors.push(err.message));
  await page.goto('/');await ready(page);await page.getByRole('button',{name:'导入数据',exact:true}).click();
  await page.locator('.import-text').fill('label,longitude,latitude,depth_m\nAlpha,118,22,30\nBeta,118.01,22.01,100');
  await page.getByRole('button',{name:'导入数据',exact:true}).last().click();await expect(page.getByRole('dialog')).not.toBeVisible();await ready(page);
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
    await page.locator('.sim-main').evaluate((el:HTMLElement)=>el.scrollTop=0);await page.locator('.sim-settings').evaluate((el:HTMLElement)=>el.scrollTop=0);await page.screenshot({path:`artifacts/${kind}.png`,fullPage:true});
  }await expect(page.locator('.playback input')).toHaveAttribute('max',/[1-9][0-9]*/);await page.getByRole('button',{name:'播放计算帧',exact:true}).click();await expect(page.locator('.sim-overlay strong')).not.toHaveText('t = 0.00 s');expect(errors).toEqual([]);
});

test('mobile layout contains its tables and navigation',async({page})=>{
  await page.setViewportSize({width:390,height:844});await page.goto('/');await ready(page);
  expect(await page.evaluate(()=>document.body.scrollWidth)).toBeLessThanOrEqual(390);
  await page.screenshot({path:'artifacts/mobile.png',fullPage:true});
});
