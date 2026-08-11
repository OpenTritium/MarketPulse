// 文本资源导入声明：bun build --compile 时内嵌为字符串。
declare module "*.css" {
  const content: string;
  export default content;
}
declare module "*.js" {
  const content: string;
  export default content;
}
