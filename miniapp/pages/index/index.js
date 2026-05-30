const config = require("../../config");

const modes = [
  { label: "安全：清理元数据", value: "safe" },
  { label: "深度：重编码", value: "paranoid" },
  { label: "核弹：扰动像素", value: "nuclear" },
  { label: "去水印：智能检测", value: "watermark" }
];

Page({
  data: {
    modeIndex: 0,
    modeLabels: modes.map((item) => item.label),
    filePath: "",
    result: null,
    message: "",
    loggedIn: false,
    busy: false,
    loginBusy: false,
    downloadBusy: false
  },

  onLoad() {
    this.setData({ loggedIn: Boolean(wx.getStorageSync("imgclean_token")) });
  },

  onModeChange(event) {
    this.setData({ modeIndex: Number(event.detail.value), result: null, message: "" });
  },

  login() {
    this.setData({ loginBusy: true, message: "" });
    wx.login({
      success: ({ code }) => {
        if (!code) {
          this.setData({ loginBusy: false, message: "微信登录没有返回 code。" });
          return;
        }
        wx.request({
          url: `${config.apiBaseUrl}/api/auth/wechat-miniprogram/login`,
          method: "POST",
          data: { code },
          success: (response) => {
            if (response.statusCode !== 200) {
              const detail = response.data && response.data.detail ? response.data.detail : "小程序登录未配置。";
              this.setData({ message: detail });
              return;
            }
            wx.setStorageSync("imgclean_token", response.data.token);
            getApp().globalData.token = response.data.token;
            this.setData({ loggedIn: true, message: "登录完成。" });
          },
          fail: () => this.setData({ message: "无法连接登录服务。" }),
          complete: () => this.setData({ loginBusy: false })
        });
      },
      fail: () => this.setData({ loginBusy: false, message: "微信登录失败。" })
    });
  },

  chooseImage() {
    wx.chooseMedia({
      count: 1,
      mediaType: ["image"],
      sourceType: ["album", "camera"],
      success: (response) => {
        const file = response.tempFiles && response.tempFiles[0];
        if (!file) return;
        this.setData({ filePath: file.tempFilePath, result: null, message: "" });
      }
    });
  },

  uploadImage() {
    if (!this.data.filePath) {
      this.setData({ message: "请先选择图片。" });
      return;
    }
    this.setData({ busy: true, message: "", result: null });
    wx.uploadFile({
      url: `${config.apiBaseUrl}/api/clean`,
      filePath: this.data.filePath,
      name: "files",
      header: this.authHeader(),
      formData: {
        mode: modes[this.data.modeIndex].value
      },
      success: (response) => {
        let payload;
        try {
          payload = JSON.parse(response.data);
        } catch (error) {
          this.setData({ message: "服务返回格式异常。" });
          return;
        }
        if (response.statusCode !== 200) {
          this.setData({ message: payload.detail || "处理失败。" });
          return;
        }
        const result = payload.results && payload.results[0];
        if (!result || !result.ok) {
          this.setData({ message: (result && result.error) || "处理失败。" });
          return;
        }
        this.setData({ result, message: "处理完成。" });
      },
      fail: () => this.setData({ message: "上传失败，请检查合法域名配置。" }),
      complete: () => this.setData({ busy: false })
    });
  },

  downloadResult() {
    const result = this.data.result;
    if (!result || !result.download_url) return;
    this.setData({ downloadBusy: true, message: "" });
    wx.downloadFile({
      url: this.downloadUrl(result),
      header: this.authHeader(),
      success: (response) => {
        if (response.statusCode !== 200) {
          this.setData({ message: "下载失败。" });
          return;
        }
        wx.previewImage({ urls: [response.tempFilePath] });
      },
      fail: () => this.setData({ message: "下载失败，请检查 downloadFile 合法域名。" }),
      complete: () => this.setData({ downloadBusy: false })
    });
  },

  downloadUrl(result) {
    const url = result.download_url;
    const filename = encodeURIComponent(result.download_filename || "cleaned-image");
    if (url.indexOf("http") === 0 && url.indexOf(config.apiBaseUrl) !== 0) {
      return `${config.apiBaseUrl}/api/download?url=${encodeURIComponent(url)}&filename=${filename}`;
    }
    if (url.indexOf("/") === 0) return `${config.apiBaseUrl}${url}`;
    return url;
  },

  authHeader() {
    const token = getApp().globalData.token || wx.getStorageSync("imgclean_token");
    return token ? { Authorization: `Bearer ${token}` } : {};
  }
});
