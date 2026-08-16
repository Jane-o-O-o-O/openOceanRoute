import {mkdirSync} from 'node:fs';
import {join} from 'node:path';
/** Release regression screenshots are isolated from previously delivered images. */
export function artifactPath(name:string){const directory=process.env.OCEANROUTE_ARTIFACTS_DIR||'artifacts/release-0.4/all';mkdirSync(directory,{recursive:true});return join(directory,name)}
