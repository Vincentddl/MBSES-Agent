/* Real HTTP/UI round trip. Requires mock models; never enables paid calls. */
const {chromium} = require('playwright');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

(async () => {
  const base = process.env.DEMO_URL || 'http://127.0.0.1:8017';
  const health = await (await fetch(base + '/api/health')).json();
  assert.equal(health.backends.llm, 'mock');
  assert.equal(health.backends.embedding, 'mock');
  const browser = await chromium.launch({channel:'msedge', headless:true});
  try {
    const page = await browser.newPage({viewport:{width:1440,height:1000}});
    const errors = [];
    page.on('pageerror', e => errors.push(String(e)));
    page.on('dialog', d => d.accept());
    await page.goto(base);
    await page.locator('#newProject').click();
    await page.locator('#projectName').fill('Agent memory verification ' + Date.now());
    await page.locator('#createProject').click();
    await page.locator('#projectDialog').waitFor({state:'hidden'});
    await page.locator('#file').setInputFiles({name:'requirements.txt', mimeType:'text/plain',
      buffer:Buffer.from('自动刷牙，需求编号1.1.1。更换刷头，需求编号1.1.2。质量要求，重量不超过200克。防水，可用水冲洗整个牙刷。')});
    await page.locator('#upload').click();
    await page.locator('#uploadStatus').filter({hasText:'1/1 成功'}).waitFor();
    await page.locator('#requirement').fill('提取自动刷牙、更换刷头、质量要求和防水需求');
    await page.locator('#generate').click();
    await page.locator('#taskStatus').filter({hasText:'待人工审核'}).waitFor();
    const firstId = await page.locator('#taskId').inputValue();
    const first = JSON.parse(await page.locator('#draft').inputValue());
    await page.locator('#conversationStatus').filter({hasText:'1 轮'}).waitFor();
    await page.locator('#requirement').fill('将质量要求改为不超过180克，其他保持不变');
    await page.locator('#generate').click();
    await page.waitForFunction(id=>document.getElementById('taskId').value!==id && document.getElementById('taskId').value.length===32,firstId);
    await page.locator('#taskStatus').filter({hasText:'待人工审核'}).waitFor();
    const secondId = await page.locator('#taskId').inputValue();
    assert.notEqual(firstId, secondId);
    const second = JSON.parse(await page.locator('#draft').inputValue());
    assert.equal(second.model_elements.find(e=>e.localId==='mass').properties.value,180);
    assert.deepEqual(first.sysml_relationships,second.sysml_relationships);
    assert.equal(await page.locator('#confirm').isDisabled(),true);
    await page.locator('#conversationStatus').filter({hasText:'2 轮'}).waitFor();
    assert.equal(await page.locator('#conversationTurns li').count(),2);
    const usage = await (await fetch(base+'/api/tasks/'+secondId+'/usage')).json();
    assert(usage.some(u=>u.stage==='tool_call'&&u.detail.name==='read_previous_draft'));
    for (const [width,height,label] of [[1440,1000,'desktop'],[390,844,'mobile']]) {
      await page.setViewportSize({width,height});
      assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
      await page.screenshot({path:path.join(__dirname,'../artifacts/agent-'+label+'.png'),fullPage:true});
    }
    await page.reload();
    await page.locator('#conversationStatus').filter({hasText:'2 轮'}).waitFor();
    assert.equal(await page.locator('#taskId').inputValue(),secondId);
    await page.locator('#validate').click();
    await page.waitForFunction(()=>!document.getElementById('confirm').disabled);
    await page.locator('#confirm').click();
    await page.locator('#taskStatus').filter({hasText:'已确认'}).waitFor();
    await page.locator('#newConversation').click();
    assert.equal(await page.locator('#conversationTurns li').count(),0);
    await page.route('**/api/generate', route=>route.abort());
    await page.locator('#requirement').fill('提取自动刷牙需求');
    await page.locator('#generate').click();
    await page.waitForFunction(()=>!document.getElementById('generate').disabled);
    const storedConversation = await page.evaluate(()=>localStorage.getItem('mbses:conversation:'+document.getElementById('project').value));
    assert(storedConversation);
    await page.unroute('**/api/generate');
    await page.reload();
    await page.locator('#conversationStatus').filter({hasText:storedConversation.slice(0,8)}).waitFor();
    assert.equal(await page.locator('#conversationTurns li').count(),0);
    assert.deepEqual(errors,[]);
    const report={passed:true,backends:health.backends,paid_api_calls:0,first_task:firstId,second_task:secondId,
      flow:'upload -> draft -> follow-up edit -> reload -> validate -> human confirm -> new conversation',browser_errors:errors};
    fs.writeFileSync(path.join(__dirname,'../artifacts/agent-ui-report.json'),JSON.stringify(report,null,2));
    console.log(JSON.stringify(report));
  } finally {await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
