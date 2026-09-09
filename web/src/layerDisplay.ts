import type {Layer} from './types';

/** The open workspace contract treats an omitted visibility flag as visible. */
export function layerVisible(layer:Pick<Layer,'visible'>):boolean{
  return layer.visible===undefined||layer.visible===true;
}

/** Drawing opacity affects only display; it never changes GIS coordinates. */
export function layerOpacity(layer:Pick<Layer,'display'>|undefined):number{
  const opacity=layer?.display?.opacity;
  return typeof opacity==='number'&&Number.isFinite(opacity)&&opacity>=0&&opacity<=1?opacity:1;
}

export function layerDrawingOrder<T extends {id:string}>(rendered:T[],layers:Layer[]):T[]{
  const order=new Map(layers.map((layer,index)=>[layer.id,index]));
  return [...rendered].sort((a,b)=>(order.get(a.id)??-1)-(order.get(b.id)??-1));
}
