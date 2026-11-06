declare module "virtual:openbot-plugins" {
  const plugins: import("./types").FrontendPlugin[];
  export default plugins;
}
