import { createApp } from "vue";
import App from "./App.vue";
import router from "./router";
import "./i18n/locales/common";
import "./i18n/locales/workspace";
import "./style.css";

const app = createApp(App);
app.use(router);
app.mount("#app");
