<!--
  Copyright (C) 2026 Genius Dynamics
  SPDX-License-Identifier: GPL-3.0-or-later
-->
<template>
  <cv-grid fullWidth>
    <cv-row>
      <cv-column class="page-title">
        <h2>{{ $t("openbao.title") }}</h2>
      </cv-column>
    </cv-row>
    <cv-row v-if="error.getConfiguration">
      <cv-column>
        <NsInlineNotification
          kind="error"
          :title="$t('action.get-configuration')"
          :description="error.getConfiguration"
          :showCloseButton="false"
        />
      </cv-column>
    </cv-row>
    <cv-row>
      <cv-column>
        <cv-tile light>
          <p class="mg-bottom">{{ $t("openbao.description") }}</p>
          <NsInlineNotification
            v-if="!loading.getConfiguration && !role"
            kind="info"
            :title="$t('openbao.not_configured')"
            :description="$t('openbao.not_configured_description')"
            :showCloseButton="false"
          />
          <cv-form @submit.prevent="save">
            <NsToggle
              value="vault"
              :label="$t('openbao.enabled')"
              v-model="vault_enabled"
              :disabled="busy"
              class="mg-bottom"
            >
              <template #tooltip>{{ $t("openbao.enabled_tooltip") }}</template>
              <template slot="text-left">{{ $t("common.disabled") }}</template>
              <template slot="text-right">{{ $t("common.enabled") }}</template>
            </NsToggle>
            <template v-if="vault_enabled">
              <cv-radio-group
                vertical
                :legend-text="$t('openbao.source')"
                class="mg-bottom"
              >
                <cv-radio-button
                  v-model="source"
                  value="cluster"
                  :label="$t('openbao.source_cluster')"
                  :disabled="busy || !servers.length"
                />
                <cv-radio-button
                  v-model="source"
                  value="manual"
                  :label="$t('openbao.source_manual')"
                  :disabled="busy"
                />
              </cv-radio-group>
              <NsInlineNotification
                v-for="item in unpublished"
                :key="item.module_id"
                kind="info"
                :title="
                  $t('openbao.unpublished_title', {
                    name: item.ui_name || item.module_id,
                  })
                "
                :description="$t('openbao.unpublished_description')"
                :showCloseButton="false"
                class="maxwidth"
              />
              <NsInlineNotification
                v-if="
                  !loading.servers && !servers.length && !unpublished.length
                "
                kind="info"
                :title="$t('openbao.no_cluster_instance')"
                :description="$t('openbao.no_cluster_instance_description')"
                :showCloseButton="false"
                class="maxwidth"
              />
              <cv-select
                v-if="source === 'cluster' && servers.length"
                :label="$t('openbao.discovered')"
                v-model="vault_address"
                :disabled="busy"
                class="mg-bottom maxwidth"
              >
                <cv-select-option
                  v-for="server in servers"
                  :key="server.module_id + server.url"
                  :value="server.url"
                  >{{ server.ui_name || server.module_id }} ({{
                    server.url
                  }})</cv-select-option
                >
              </cv-select>
              <NsTextInput
                v-if="source === 'manual'"
                :label="$t('openbao.address')"
                v-model.trim="vault_address"
                placeholder="https://vault.example.org"
                :helper-text="$t('openbao.address_helper')"
                :invalid-message="error.vault_address"
                :disabled="busy"
                ref="vault_address"
                class="mg-bottom maxwidth"
              />
              <NsTextInput
                type="password"
                autocomplete="new-password"
                :label="$t('openbao.token')"
                v-model="vault_token"
                :placeholder="vault_token_set ? $t('common.unchanged') : ''"
                :helper-text="$t('openbao.token_helper')"
                :invalid-message="error.vault_token"
                :disabled="busy"
                ref="vault_token"
                class="mg-bottom maxwidth"
              />
              <NsTextInput
                :label="$t('openbao.mount')"
                v-model.trim="vault_mount"
                :invalid-message="error.vault_mount"
                :disabled="busy"
                ref="vault_mount"
                class="mg-bottom narrow"
              />
              <NsTextInput
                :label="$t('openbao.path')"
                v-model.trim="vault_path"
                :helper-text="$t('openbao.path_helper', { path: vault_path })"
                :invalid-message="error.vault_path"
                :disabled="busy"
                ref="vault_path"
                class="mg-bottom maxwidth"
              />
            </template>
            <NsInlineNotification
              v-if="unavailableItems.length"
              kind="warning"
              :title="$t('openbao.items_unavailable_title')"
              :description="
                $t('openbao.items_unavailable_description', {
                  items: unavailableItems.join(', '),
                })
              "
              :showCloseButton="false"
            />
            <NsInlineNotification
              v-if="error.configureModule"
              kind="error"
              :title="$t('action.configure-module')"
              :description="error.configureModule"
              :showCloseButton="false"
            />
            <NsButton
              kind="primary"
              :icon="Save20"
              :loading="loading.configureModule"
              :disabled="busy || !role"
              >{{ $t("settings.save") }}</NsButton
            >
          </cv-form>
        </cv-tile>
      </cv-column>
    </cv-row>
    <!-- turning OpenBao off: secrets that exist only in OpenBao -->
    <NsModal
      size="default"
      kind="danger"
      :visible="disableModal.visible"
      :primaryButtonDisabled="!disableModal.copyable || loading.copy"
      :isLoading="loading.copy"
      @modal-hidden="disableModal.visible = false"
      @other-btn-click="disableModal.visible = false"
      @secondary-click="disableAnyway"
      @primary-click="copyThenDisable"
    >
      <template slot="title">{{ $t("openbao.disable_title") }}</template>
      <template slot="content">
        <NsInlineNotification
          kind="warning"
          :title="$t('openbao.disable_warning_title')"
          :description="$t('openbao.disable_warning_description')"
          :showCloseButton="false"
        />
        <ul class="items mg-bottom">
          <li v-for="item in disableModal.items" :key="item.kind + item.name">
            <strong>{{ $t("openbao.item_" + item.kind) }}</strong>
            <span>: {{ item.name }}</span>
            <span class="path">({{ item.path }})</span>
            <NsTag
              v-if="!item.readable"
              kind="red"
              :label="$t('openbao.not_readable')"
            />
          </li>
        </ul>
        <p v-if="disableModal.copyable">
          {{ $t("openbao.copy_description") }}
        </p>
        <NsInlineNotification
          v-else
          kind="error"
          :title="$t('openbao.copy_impossible')"
          :description="$t('openbao.copy_impossible_description')"
          :showCloseButton="false"
        />
        <NsInlineNotification
          v-if="error.copy"
          kind="error"
          :title="$t('action.copy-openbao-to-local')"
          :description="error.copy"
          :showCloseButton="false"
        />
      </template>
      <template slot="other-button">{{ $t("common.cancel") }}</template>
      <template slot="secondary-button">{{
        $t("openbao.disable_anyway")
      }}</template>
      <template slot="primary-button">{{
        $t("openbao.copy_and_disable")
      }}</template>
    </NsModal>
  </cv-grid>
</template>

<script>
import { mapState } from "vuex";
import {
  QueryParamService,
  UtilService,
  TaskService,
  IconService,
  PageTitleService,
} from "@nethserver/ns8-ui-lib";
import ModuleTask from "@/mixins/moduleTask";

export default {
  name: "OpenBao",
  mixins: [
    TaskService,
    IconService,
    UtilService,
    QueryParamService,
    PageTitleService,
    ModuleTask,
  ],
  pageTitle() {
    return this.$t("openbao.title") + " - " + this.appName;
  },
  data() {
    return {
      q: { page: "openbao" },
      urlCheckInterval: null,
      vault_enabled: false,
      // vault_enabled as saved: the warning applies when it goes off
      vault_enabled_saved: false,
      disableModal: { visible: false, items: [], copyable: false },
      // configure-module warning: secrets left behind in OpenBao
      unavailableItems: [],
      vault_address: "",
      vault_token: "",
      vault_token_set: false,
      vault_mount: "secret",
      vault_path: "postgresql19c",
      role: "",
      clusterId: "",
      servers: [],
      unpublished: [],
      // "cluster": an OpenBao of the cluster, "manual": any URL
      source: "manual",
      loading: {
        servers: true,
        getConfiguration: false,
        configureModule: false,
        copy: false,
      },
      error: {
        getConfiguration: "",
        configureModule: "",
        copy: "",
        vault_address: "",
        vault_token: "",
        vault_mount: "",
        vault_path: "",
      },
    };
  },
  computed: {
    ...mapState(["instanceName", "core", "appName"]),
    busy() {
      return this.loading.getConfiguration || this.loading.configureModule;
    },
  },
  watch: {
    source(value) {
      const urls = this.servers.map((s) => s.url);
      if (
        value === "cluster" &&
        urls.length &&
        !urls.includes(this.vault_address)
      ) {
        this.vault_address = urls[0];
      }
    },
  },
  created() {
    this.getConfiguration();
    this.listServers();
  },
  beforeRouteEnter(to, from, next) {
    next((vm) => {
      vm.watchQueryData(vm);
      vm.urlCheckInterval = vm.initUrlBindingForApp(vm, vm.q.page);
    });
  },
  beforeRouteLeave(to, from, next) {
    clearInterval(this.urlCheckInterval);
    next();
  },
  methods: {
    async getConfiguration() {
      this.loading.getConfiguration = true;
      this.error.getConfiguration = "";
      try {
        const c = await this.runModuleTask("get-configuration");
        this.vault_enabled = c.vault_enabled;
        this.vault_enabled_saved = c.vault_enabled;
        this.vault_address = c.vault_address;
        if (!this.loading.servers) {
          this.chooseSource();
        }
        this.vault_token_set = c.vault_token_set;
        this.vault_mount = c.vault_mount;
        this.vault_path = c.vault_path;
        this.role = c.role;
        this.clusterId = c.cluster_id;
      } catch (failure) {
        this.error.getConfiguration = this.taskErrorMessage(failure);
      }
      this.loading.getConfiguration = false;
    },
    async listServers() {
      this.loading.servers = true;
      try {
        const out = await this.runModuleTask("list-openbao-servers");
        this.servers = out.servers || [];
        this.unpublished = out.unpublished || [];
      } catch (failure) {
        this.servers = [];
        this.unpublished = [];
      }
      this.loading.servers = false;
      this.chooseSource();
    },
    chooseSource() {
      // An address of a cluster instance, or no address yet with
      // instances to offer: the cluster choice
      const urls = this.servers.map((s) => s.url);
      if (urls.includes(this.vault_address)) {
        this.source = "cluster";
      } else if (!this.vault_address && urls.length) {
        this.source = "cluster";
        this.vault_address = urls[0];
      } else {
        this.source = "manual";
      }
    },
    async save() {
      this.clearErrors(this);
      if (this.vault_enabled_saved && !this.vault_enabled) {
        // Turning OpenBao off: warn about the secrets that exist only there
        this.loading.configureModule = true;
        let listing = null;
        try {
          listing = await this.runModuleTask("list-openbao-only-secrets");
        } catch (failure) {
          this.error.configureModule = this.taskErrorMessage(failure);
          this.loading.configureModule = false;
          return;
        }
        this.loading.configureModule = false;
        if (listing.items.length) {
          this.disableModal = {
            visible: true,
            items: listing.items,
            copyable: listing.copyable,
          };
          return;
        }
      }
      await this.configure();
    },
    async disableAnyway() {
      this.disableModal.visible = false;
      await this.configure();
    },
    async copyThenDisable() {
      this.error.copy = "";
      this.loading.copy = true;
      try {
        await this.runModuleTask(
          "copy-openbao-to-local",
          {},
          { title: this.$t("action.copy-openbao-to-local"), hidden: false }
        );
      } catch (failure) {
        this.error.copy = this.taskErrorMessage(failure);
        this.loading.copy = false;
        return;
      }
      this.loading.copy = false;
      this.disableModal.visible = false;
      await this.configure();
    },
    async configure() {
      if (this.vault_enabled && !this.vault_address) {
        this.error.vault_address = this.$t("common.required");
        this.focusElement("vault_address");
        return;
      }
      if (this.vault_enabled && !this.vault_token && !this.vault_token_set) {
        this.error.vault_token = this.$t("common.required");
        this.focusElement("vault_token");
        return;
      }
      // configure-module needs the role: OpenBao keeps the current one
      const data = {
        role: this.role,
        vault_enabled: this.vault_enabled,
      };
      if (this.role === "join" || this.role === "witness") {
        data.cluster_id = this.clusterId;
      }
      if (this.vault_enabled) {
        data.vault_address = this.vault_address;
        data.vault_mount = this.vault_mount || "secret";
        data.vault_path = this.vault_path || "postgresql19c";
      }
      if (this.vault_token) {
        data.vault_token = this.vault_token;
      }
      this.loading.configureModule = true;
      try {
        const out = await this.runModuleTask("configure-module", data, {
          title: this.$t("openbao.saving"),
          hidden: false,
        });
        const lost = ((out && out.warnings) || []).find(
          (w) => w.id === "openbao_items_unavailable"
        );
        this.unavailableItems = lost ? lost.items : [];
        this.vault_token = "";
        this.getConfiguration();
      } catch (failure) {
        this.error.configureModule = this.taskErrorMessage(failure);
      }
      this.loading.configureModule = false;
    },
  },
};
</script>

<style scoped lang="scss">
@import "../styles/carbon-utils";
.mg-bottom {
  margin-bottom: $spacing-06;
}
.maxwidth {
  max-width: 38rem;
}
.narrow {
  max-width: 16rem;
}
.label {
  font-size: 0.75rem;
  margin-bottom: $spacing-03;
}
.items {
  margin-top: $spacing-05;
  list-style: disc;
  padding-left: $spacing-06;
}
.items li {
  margin-bottom: $spacing-03;
}
.path {
  margin-left: $spacing-03;
  color: $text-02;
}
.servers {
  display: flex;
  flex-wrap: wrap;
  gap: $spacing-03;
}
</style>
