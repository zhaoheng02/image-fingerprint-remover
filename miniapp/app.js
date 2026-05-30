const config = require("./config");

App({
  globalData: {
    apiBaseUrl: config.apiBaseUrl,
    token: ""
  },
  onLaunch() {
    this.globalData.token = wx.getStorageSync("imgclean_token") || "";
  }
});
