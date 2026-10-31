/** A narrow adapter for the user-authorized iPhone Mirroring window helper.
 * execFile argument arrays only; no shell, browser profiles, AppleScript, or
 * private phone APIs. Construction/import never invokes the helper.
 */
import fs from 'node:fs/promises';
import path from 'node:path';
import {randomUUID} from 'node:crypto';
import {execFile} from 'node:child_process';
import {promisify} from 'node:util';
const executeFile = promisify(execFile);
const MIRROR_BUNDLE = 'com.apple.ScreenContinuity';

export class NativeMobileError extends Error {
  constructor(message, code='NATIVE_HELPER_ERROR') { super(message); this.code=code; }
}

export class NativeMobileApp {
  constructor({projectDir,execute=executeFile}={}) {
    if(!projectDir)throw new NativeMobileError('缺少项目目录');
    this.root=path.resolve(projectDir);this.helper=path.join(this.root,'output/bin/native-mobile');
    this.execute=execute;this.controller=new AbortController();this.lastImage=null;
  }
  async invoke(command,args=[]) {
    if(this.controller.signal.aborted)throw new NativeMobileError('手机驱动已停止','STOPPED');
    let output;
    try { output=await this.execute(this.helper,[command,...args],{
      cwd:this.root,timeout:15000,maxBuffer:1024*1024,encoding:'utf8',signal:this.controller.signal});
    } catch(error) {
      if(error.stdout)output={stdout:error.stdout};
      else throw new NativeMobileError(error.code==='ENOENT'?'本地手机驱动尚未编译':
        error.name==='AbortError'?'手机驱动已停止':'本地手机驱动执行失败',error.code||'HELPER_FAILED');
    }
    let value;
    try {value=JSON.parse(output.stdout);}catch {throw new NativeMobileError('本地手机驱动未返回有效JSON');}
    if(value.bundleIdentifier!==MIRROR_BUNDLE)throw new NativeMobileError('驱动目标不是获准的iPhone镜像窗口','WRONG_TARGET');
    if(!value.ok && command!=='health')throw new NativeMobileError(value.message||value.error||'镜像窗口操作未完成',value.code||'NOT_READY');
    return value;
  }
  health(){return this.invoke('health');}
  async getAXState() {
    const state=await this.invoke('state');
    if(state.ready===false||!state.windowId)throw new NativeMobileError(state.message||'镜像窗口或本机权限尚未就绪','NOT_READY');
    if(state.targetFrontmost===false)throw new NativeMobileError('iPhone镜像不在前台，等待本人切回；不会自动抢占前台','TARGET_BACKGROUND');
    // The helper intentionally exposes no private AX text. Screen state is
    // established separately by OCR of this exact window, never by metadata.
    return JSON.stringify({bundleIdentifier:state.bundleIdentifier,windowId:state.windowId,
      deviceUnlocked:state.deviceUnlocked??null,frame:state.frame});
  }
  async getScreenshot() {
    const directory=path.join(this.root,'data/market_evidence');
    await fs.mkdir(directory,{recursive:true,mode:0o700});
    const output=path.join(directory,`native_capture_${randomUUID()}.png`);
    try {
      const result=await this.invoke('screenshot',[output]);
      if(result.ready===false||path.resolve(result.path||'')!==output)throw new NativeMobileError('窗口截图位置未核对一致');
      const bytes=await fs.readFile(output);
      if(bytes.length<24||!bytes.subarray(0,8).equals(Buffer.from([137,80,78,71,13,10,26,10])))throw new NativeMobileError('镜像截图不是有效PNG');
      const width=bytes.readUInt32BE(16),height=bytes.readUInt32BE(20);
      if(!width||!height||width!==result.imageWidth||height!==result.imageHeight)throw new NativeMobileError('镜像截图像素尺寸不一致');
      this.lastImage={width,height,windowId:result.windowId};
      return bytes;
    } finally {await fs.unlink(output).catch(()=>{});}
  }
  async click(point) {
    if(!Array.isArray(point)||point.length!==2||!point.every(Number.isFinite)||!this.lastImage)throw new NativeMobileError('点击前须有本窗口实际截图及有限像素坐标');
    const [x,y]=point.map(Math.round);
    if(x<0||y<0||x>=this.lastImage.width||y>=this.lastImage.height)throw new NativeMobileError('点击坐标超出镜像PNG');
    return this.invoke('click',[String(x),String(y)]);
  }
  async typeText(text) {
    if(!this.lastImage)throw new NativeMobileError('输入前须先核对本窗口实际截图');
    if(typeof text!=='string'||!text.trim()||text.length>160||/[\x00-\x1f\x7f]/.test(text))throw new NativeMobileError('查询须为1至160字普通文本');
    return this.invoke('type',[text]);
  }
  stop(){this.controller.abort();}
}
