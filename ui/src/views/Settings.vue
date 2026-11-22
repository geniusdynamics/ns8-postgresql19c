<!--
  Copyright (C) 2022 Nethesis S.r.l.
  Copyright (C) 2026 Genius Dynamics
  SPDX-License-Identifier: GPL-3.0-or-later
-->
<template>
  <cv-grid fullWidth>
    <cv-row>
      <cv-column class="page-title">
        <h2>{{ $t("settings.title") }}</h2>
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
          <cv-skeleton-text
            v-if="loading.getConfiguration"
            :paragraph="true"
            :line-count="10"
          ></cv-skeleton-text>
          <cv-form v-else @submit.prevent="configureModule">
            <cv-radio-group
              :legend="$t('settings.role')"
              vertical
              class="mg-bottom"
              ref="role"
            >
              <template v-for="r in roles">
                <cv-radio-button
                  :key="'radio-' + r"
                  :label="$t('settings.role_' + r)"
                  :value="r"
                  v-model="role"
                  :disabled="isFormDisabled || !canChooseRole(r)"
                />
                <p :key="'desc-' + r" class="radio-description">
                  {{ $t("settings.role_" + r + "_description") }}
                </p>
              </template>
            </cv-radio-group>
            <div v-if="isJoining" class="maxwidth mg-bottom">
              <NsComboBox
                v-model="clusterId"
                :options="candidateOptions"
                :title="$t('settings.cluster_id')"
                :label="$t('settings.cluster_id_placeholder')"
                :invalid-message="error.cluster_id"
                :disabled="isFormDisabled || savedRole === role"
                :auto-filter="true"
                :auto-highlight="true"
                class="mg-bottom"
                ref="cluster_id"
              />
              <NsInlineNotification
                v-if="!candidateOptions.length && savedRole !== role"
                kind="info"
                :title="$t('settings.no_clusters')"
                :description="$t('settings.no_clusters_description')"
                :showCloseButton="false"
              />
              <NsInlineNotification
                v-if="savedRole !== role"
                kind="info"
                :title="$t('settings.approval_needed')"
                :description="
                  $t('settings.approval_needed_description', {
                    instance: instanceName,
                  })
                "
                :showCloseButton="false"
              />
            </div>
            <NsInlineNotification
              v-if="role !== 'standalone' && expectedVoters < 3"
              kind="warning"
              :title="$t('settings.quorum_warning')"
              :description="
                $t('settings.quorum_warning_description', {
                  count: expectedVoters,
                })
              "
              :showCloseButton="false"
            />
            <NsInlineNotification
              v-if="isLeaving"
              kind="warning"
              :title="$t('settings.leave_warning')"
              :description="$t('settings.leave_warning_description')"
              :showCloseButton="false"
            />
            <NsInlineNotification
              v-if="role === 'join' && savedRole === 'standalone'"
              kind="warning"
              :title="$t('settings.join_wipes_title')"
              :description="$t('settings.join_wipes_description')"
              :showCloseButton="false"
            />
            <cv-radio-group
              v-if="role !== 'witness'"
              :legend="$t('settings.replication_mode')"
              vertical
              class="mg-bottom"
            >
              <template v-for="mode in ['async', 'sync']">
                <cv-radio-button
                  :key="'mode-' + mode"
                  :label="$t('settings.replication_' + mode)"
                  :value="mode"
                  v-model="replicationMode"
                  :disabled="isFormDisabled || isClusterSettingLocked"
                />
                <p :key="'mode-desc-' + mode" class="radio-description">
                  {{ $t("settings.replication_" + mode + "_description") }}
                </p>
              </template>
            </cv-radio-group>
            <!-- members allowed to join this cluster -->
            <div v-if="isClusterMember" class="mg-bottom maxwidth">
              <h6 class="mg-bottom-sm">{{ $t("settings.peer_access") }}</h6>
              <p class="helper mg-bottom-sm">
                {{ $t("settings.peer_access_description") }}
              </p>
              <NsEmptyState
                v-if="!peerCandidates.length"
                :title="$t('settings.no_peer_candidates')"
              />
              <div
                v-for="peer in peerCandidates"
                :key="peer.module_id"
                class="peer"
              >
                <NsToggle
                  :value="peer.module_id"
                  :form-item="true"
                  :label="peerLabel(peer)"
                  :checked="peer.allowed"
                  :disabled="loading.peerAccess || isFormDisabled"
                  @change="setPeerAccess(peer, $event)"
                >
                  <template slot="text-left">{{
                    $t("settings.not_allowed")
                  }}</template>
                  <template slot="text-right">{{
                    $t("settings.allowed")
                  }}</template>
                </NsToggle>
              </div>
              <NsInlineNotification
                v-if="error.peerAccess"
                kind="error"
                :title="$t('action.set-peer-access')"
                :description="error.peerAccess"
                :showCloseButton="false"
              />
            </div>
            <!-- advanced options -->
            <cv-accordion ref="accordion" class="maxwidth mg-bottom">
              <cv-accordion-item :open="toggleAccordion[0]">
                <template slot="title">{{ $t("settings.advanced") }}</template>
                <template slot="content">
                  <NsTextInput
                    v-model.trim="maxConnections"
                    type="number"
                    min="20"
                    max="5000"
                    :label="$t('settings.max_connections')"
                    :helper-text="$t('settings.max_connections_helper')"
                    :invalid-message="error.max_connections"
                    :disabled="isFormDisabled || isClusterSettingLocked"
                    ref="max_connections"
                  />
                  <NsToggle
                    value="pgadmin"
                    :label="$t('settings.pgadmin_enabled')"
                    v-model="pgadminEnabled"
                    :disabled="isFormDisabled"
                    class="mg-bottom"
                  >
                    <template #tooltip>{{
                      $t("settings.pgadmin_tooltip")
                    }}</template>
                    <template slot="text-left">{{
                      $t("common.disabled")
                    }}</template>
                    <template slot="text-right">{{
                      $t("common.enabled")
                    }}</template>
                  </NsToggle>
                  <template v-if="pgadminEnabled">
                    <NsTextInput
                      v-model.trim="host"
                      :label="$t('settings.pgadmin_host')"
                      placeholder="pgadmin.example.org"
                      :invalid-message="error.host"
                      :disabled="isFormDisabled"
                      ref="host"
                    />
                    <NsTextInput
                      v-model.trim="pgadminEmail"
                      :label="$t('settings.pgadmin_email')"
                      :helper-text="$t('settings.pgadmin_email_helper')"
                      :invalid-message="error.pgadmin_email"
                      :disabled="isFormDisabled"
                      ref="pgadmin_email"
                    />
                    <NsToggle
                      value="letsEncrypt"
                      :label="$t('settings.lets_encrypt')"
                      v-model="letsEncrypt"
                      :disabled="isFormDisabled"
                    >
                      <template slot="text-left">{{
                        $t("common.disabled")
                      }}</template>
                      <template slot="text-right">{{
                        $t("common.enabled")
                      }}</template>
                    </NsToggle>
                    <NsTextInput
                      v-model.trim="pgadminPath"
                      :label="$t('settings.pgadmin_path')"
                      placeholder="/pgadmin"
                      :helper-text="$t('settings.pgadmin_path_helper')"
                      :invalid-message="error.pgadmin_path"
                      :disabled="isFormDisabled"
                      ref="pgadmin_path"
                    />
                    <cv-text-area
                      v-model="pgadminAllowlist"
                      :label="$t('settings.pgadmin_ip_allowlist')"
                      :helper-text="$t('settings.allowlist_helper')"
                      :invalid-message="error.pgadmin_ip_allowlist"
                      :disabled="isFormDisabled"
                      class="mg-bottom"
                      ref="pgadmin_ip_allowlist"
                    />
                    <div v-if="savedPgadmin" class="mg-bottom">
                      <NsButton
                        kind="tertiary"
                        size="field"
                        :icon="Password20"
                        :loading="loading.reveal"
                        :disabled="isFormDisabled || loading.reveal"
                        @click.prevent="revealPgadminPassword"
                        >{{ $t("settings.reveal_pgadmin_password") }}</NsButton
                      >
                      <NsCodeSnippet
                        v-if="pgadminPassword"
                        :copyTooltip="core.$t('common.copy_to_clipboard')"
                        :copy-feedback="core.$t('common.copied_to_clipboard')"
                        :feedback-aria-label="
                          core.$t('common.copied_to_clipboard')
                        "
                        :wrap-text="true"
                        :moreText="core.$t('common.show_more')"
                        :lessText="core.$t('common.show_less')"
                        hideExpandButton
                        light
                        class="mg-top-sm"
                        >{{ pgadminPassword }}</NsCodeSnippet
                      >
                    </div>
                  </template>
                </template>
              </cv-accordion-item>
            </cv-accordion>
            <!-- network access of this instance -->
            <div class="maxwidth mg-bottom">
              <h6 class="mg-bottom-sm">{{ $t("settings.network_access") }}</h6>
              <p class="helper mg-bottom-sm">
                {{ $t("settings.network_access_description") }}
              </p>
              <NsToggle
                value="publicAccess"
                :label="$t('settings.public_access_enabled')"
                v-model="publicAccess"
                :disabled="isFormDisabled"
                class="mg-bottom"
              >
                <template #tooltip>{{
                  $t("settings.public_access_tooltip")
                }}</template>
                <template slot="text-left">{{
                  $t("common.disabled")
                }}</template>
                <template slot="text-right">{{
                  $t("common.enabled")
                }}</template>
              </NsToggle>
              <template v-if="publicAccess">
                <cv-text-area
                  v-model="publicAllowlist"
                  :label="$t('settings.public_access_allowlist')"
                  :helper-text="$t('settings.allowlist_helper')"
                  :invalid-message="error.public_access_allowlist"
                  :disabled="isFormDisabled"
                  class="mg-bottom"
                  ref="public_access_allowlist"
                />
                <NsInlineNotification
                  v-if="!linesToList(publicAllowlist).length"
                  kind="warning"
                  :title="$t('settings.public_any_source')"
                  :description="$t('settings.public_any_source_description')"
                  :showCloseButton="false"
                />
                <p class="helper mg-bottom-sm">
                  {{
                    $t("settings.public_access_connect", {
                      port: publicPort,
                    })
                  }}
                </p>
                <NsCodeSnippet
                  v-if="caCertificate"
                  :copyTooltip="core.$t('common.copy_to_clipboard')"
                  :copy-feedback="core.$t('common.copied_to_clipboard')"
                  :feedback-aria-label="core.$t('common.copied_to_clipboard')"
                  :wrap-text="true"
                  :moreText="core.$t('common.show_more')"
                  :lessText="core.$t('common.show_less')"
                  light
                  class="mg-bottom"
                  >{{ caCertificate }}</NsCodeSnippet
                >
              </template>
            </div>
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
              :disabled="isFormDisabled"
              >{{ $t("settings.save") }}</NsButton
            >
          </cv-form>
        </cv-tile>
      </cv-column>
    </cv-row>
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
  name: "Settings",
  mixins: [
    TaskService,
    IconService,
    UtilService,
    QueryParamService,
    PageTitleService,
    ModuleTask,
  ],
  pageTitle() {
    return this.$t("settings.title") + " - " + this.appName;
  },
  data() {
    return {
      q: {
        page: "settings",
      },
      urlCheckInterval: null,
      roles: ["standalone", "initial", "join", "witness"],
      role: "standalone",
      savedRole: "",
      clusterId: "",
      replicationMode: "async",
      maxConnections: "300",
      members: [],
      candidates: [],
      peerCandidates: [],
      pgadminEnabled: false,
      savedPgadmin: false,
      pgadminPassword: "",
      host: "",
      pgadminEmail: "",
      letsEncrypt: false,
      pgadminPath: "",
      pgadminAllowlist: "",
      publicAccess: false,
      publicAllowlist: "",
      publicPort: 0,
      caCertificate: "",
      vault: {},
      loading: {
        getConfiguration: false,
        configureModule: false,
        peerAccess: false,
        reveal: false,
      },
      error: {
        getConfiguration: "",
        configureModule: "",
        peerAccess: "",
        role: "",
        cluster_id: "",
        max_connections: "",
        host: "",
        pgadmin_email: "",
        pgadmin_path: "",
        pgadmin_ip_allowlist: "",
        public_access_allowlist: "",
      },
    };
  },
  computed: {
    ...mapState(["instanceName", "core", "appName"]),
    isFormDisabled() {
      return this.loading.getConfiguration || this.loading.configureModule;
    },
    isJoining() {
      return this.role === "join" || this.role === "witness";
    },
    isClusterMember() {
      return ["initial", "join", "witness"].includes(this.savedRole);
    },
    isLeaving() {
      return (
        this.isClusterMember &&
        this.role === "standalone" &&
        this.members.length > 1
      );
    },
    isClusterSettingLocked() {
      // A joining member takes the settings of its cluster
      return this.isJoining && this.savedRole !== this.role;
    },
    candidateOptions() {
      return this.candidates.map((c) => ({
        name: c.cluster_id,
        value: c.cluster_id,
        label: this.$t("settings.cluster_option", {
          cluster: c.cluster_id,
          members: c.members.join(", "),
        }),
      }));
    },
    expectedVoters() {
      if (this.isClusterMember && this.savedRole === this.role) {
        return this.members.length;
      }
      if (this.role === "initial") {
        return Math.max(this.members.length, 1);
      }
      if (this.isJoining) {
        const cluster = this.candidates.find(
          (c) => c.cluster_id === this.clusterId
        );
        return cluster ? cluster.members.length + 1 : 1;
      }
      return 1;
    },
  },
  created() {
    this.getConfiguration();
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
    canChooseRole(role) {
      // Allowed changes, as checked by configure-module
      const saved = this.savedRole;
      if (!saved || saved === "standalone" || saved === role) {
        return true;
      }
      return role === "standalone";
    },
    peerLabel(peer) {
      const name = peer.ui_name
        ? `${peer.ui_name} (${peer.module_id})`
        : peer.module_id;
      return this.$t("settings.peer_label", { name, node: peer.node });
    },
    async getConfiguration() {
      this.loading.getConfiguration = true;
      this.error.getConfiguration = "";
      try {
        const config = await this.runModuleTask("get-configuration");
        this.savedRole = config.role;
        this.role = config.role || "standalone";
        this.clusterId = config.role === "standalone" ? "" : config.cluster_id;
        this.replicationMode = config.replication_mode || "async";
        this.maxConnections = String(config.max_connections || 300);
        this.members = config.members || [];
        this.candidates = config.cluster_candidates || [];
        this.peerCandidates = config.peer_candidates || [];
        this.pgadminEnabled = config.pgadmin_enabled;
        this.savedPgadmin = config.pgadmin_enabled;
        this.host = config.host || "";
        this.pgadminEmail = config.pgadmin_email || "";
        this.letsEncrypt = config.lets_encrypt;
        this.pgadminPath = config.pgadmin_path || "";
        this.pgadminAllowlist = (config.pgadmin_ip_allowlist || []).join("\n");
        this.publicAccess = config.public_access_enabled;
        this.publicAllowlist = (config.public_access_allowlist || []).join(
          "\n"
        );
        this.publicPort = config.public_port;
        this.caCertificate = config.tls_ca_certificate || "";
      } catch (failure) {
        this.error.getConfiguration = this.taskErrorMessage(failure);
      }
      this.loading.getConfiguration = false;
    },
    validateConfigureModule() {
      this.clearErrors(this);
      let ok = true;
      if (this.isJoining && !this.clusterId) {
        this.error.cluster_id = this.$t("common.required");
        this.focusElement("cluster_id");
        ok = false;
      }
      const conn = Number(this.maxConnections);
      if (!Number.isInteger(conn) || conn < 20 || conn > 5000) {
        this.error.max_connections = this.$t(
          "settings.max_connections_invalid"
        );
        ok = false;
      }
      if (this.pgadminEnabled && !this.host && !this.pgadminPath) {
        this.error.host = this.$t("common.required");
        ok = false;
      }
      return ok;
    },
    async configureModule() {
      if (!this.validateConfigureModule()) {
        return;
      }
      const data = {
        role: this.role,
        pgadmin_enabled: this.pgadminEnabled,
      };
      if (this.isJoining) {
        data.cluster_id = this.clusterId;
      }
      data.public_access_enabled = this.publicAccess;
      data.public_access_allowlist = this.publicAccess
        ? this.linesToList(this.publicAllowlist)
        : [];
      if (!this.isClusterSettingLocked) {
        data.replication_mode = this.replicationMode;
        data.max_connections = Number(this.maxConnections);
      }
      if (this.pgadminEnabled) {
        data.host = this.host;
        data.lets_encrypt = this.letsEncrypt;
        data.pgadmin_path = this.pgadminPath;
        data.pgadmin_ip_allowlist = this.linesToList(this.pgadminAllowlist);
        if (this.pgadminEmail) {
          data.pgadmin_email = this.pgadminEmail;
        }
      }
      this.loading.configureModule = true;
      try {
        await this.runModuleTask("configure-module", data, {
          title: this.$t("settings.instance_configuration", {
            instance: this.instanceName,
          }),
          description: this.$t("settings.configuring"),
          hidden: false,
        });
        this.getConfiguration();
      } catch (failure) {
        this.error.configureModule = this.taskErrorMessage(failure);
      }
      this.loading.configureModule = false;
    },
    async setPeerAccess(peer, allowed) {
      this.error.peerAccess = "";
      this.loading.peerAccess = true;
      try {
        await this.runModuleTask("set-peer-access", {
          peer: peer.module_id,
          allowed: !!allowed,
        });
        peer.allowed = !!allowed;
      } catch (failure) {
        this.error.peerAccess = this.taskErrorMessage(failure);
      }
      this.loading.peerAccess = false;
    },
    async revealPgadminPassword() {
      this.loading.reveal = true;
      try {
        const out = await this.runModuleTask("reveal-pgadmin-password");
        this.pgadminPassword = out.password;
      } catch (failure) {
        this.error.configureModule = this.taskErrorMessage(failure);
      }
      this.loading.reveal = false;
    },
  },
};
</script>

<style scoped lang="scss">
@import "../styles/carbon-utils";
.mg-bottom {
  margin-bottom: $spacing-06;
}

.mg-bottom-sm {
  margin-bottom: $spacing-04;
}

.mg-top-sm {
  margin-top: $spacing-04;
}

.maxwidth {
  max-width: 38rem;
}

.helper {
  color: $text-02;
}

.radio-description {
  max-width: 38rem;
  margin: 0 0 $spacing-05 $spacing-07;
  color: $text-02;
}
</style>
