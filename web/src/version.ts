import product from '../package.json';

/** The UI and fallback export names follow the shipped package version. */
export const productVersion=product.version;
export const productRelease=productVersion.split('.').slice(0,2).join('.');
export const productName='OceanRoute';
export const productLabel=`${productName} ${productRelease}`;
