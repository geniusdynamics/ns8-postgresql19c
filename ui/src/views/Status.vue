<!--
  Copyright (C) 2023 Nethesis S.r.l.
  SPDX-License-Identifier: GPL-3.0-or-later
-->
<template>
  <cv-grid fullWidth>
    <cv-row>
      <cv-column class="page-title">
        <h2>{{ $t("status.title") }}</h2>
      </cv-column>
    </cv-row>
    <cv-row v-if="error.getStatus">
      <cv-column>
        <NsInlineNotification
          kind="error"
          :title="$t('action.get-status')"
          :description="error.getStatus"
          :showCloseButton="false"
        />
      </cv-column>
    </cv-row>
    <cv-row v-if="error.listBackupRepositories">
      <cv-column>
        <NsInlineNotification
          kind="error"
          :title="$t('action.list-backup-repositories')"
          :description="error.listBackupRepositories"
          :showCloseButton="false"
        />
      </cv-column>
    </cv-row>
    <cv-row v-if="error.listBackups">
      <cv-column>
        <NsInlineNotification
          kind="error"
          :title="$t('action.list-backups')"
          :description="error.listBackups"
          :showCloseButton="false"
        />
      </cv-column>
    </cv-row>
    <cv-row>
      <cv-column :md="4" :max="4">
        <NsInfoCard
          light
          :title="endpointTitle"
          :description="$t('status.client_endpoint')"
          :icon="DataBase32"
          :loading="loading.getConfiguration"
          :isErrorShown="error.getConfiguration"
          :errorTitle="$t('error.cannot_retrieve_configuration')"
          :errorDescription="error.getConfiguration"
          class="min-height-card"
        >
          <template slot="content">
            <NsButton
              v-if="!config.configured"
              kind="ghost"
              :disabled="loading.getConfiguration"
              :icon="ArrowRight20"
              @click="goToAppPage(instanceName, 'settings')"
            >
              {{ $t("status.configure") }}
            </NsButton>
            <NsButton
              v-else-if="config.pgadmin_enabled && config.host"
              kind="ghost"
              :icon="Launch20"
              @click="goToPgadmin"
            >
              {{ $t("status.open_pgadmin") }}
            </NsButton>
          </template>
        </NsInfoCard>
      </cv-column>
      <cv-column :md="4" :max="4">
        <NsInfoCard
          light
          :title="config.role ? $t('settings.role_' + config.role) : '-'"
          :description="
            config.cluster_id
              ? $t('status.cluster_name', { cluster: config.cluster_id })
              : $t('status.role')
          "
          :icon="Network_232"
          :loading="loading.getConfiguration"
          class="min-height-card"
        />
      </cv-column>
      <cv-column :md="4" :max="4">
        <NsInfoCard
          light
          :title="status.instance || '-'"
          :description="$t('status.app_instance')"
          :icon="Application32"
          :loading="loading.getStatus || loading.getConfiguration"
          class="min-height-card"
        />
      </cv-column>
      <cv-column :md="4" :max="4">
        <NsInfoCard
          light
          :title="installationNodeTitle"
          :titleTooltip="installationNodeTitleTooltip"
          :description="$t('status.installation_node')"
          :icon="Chip32"
          :loading="loading.getStatus || loading.getConfiguration"
          class="min-height-card"
        />
      </cv-column>
      <cv-column :md="4" :max="4">
        <NsBackupCard
          :title="core.$t('backup.title')"
          :noBackupMessage="core.$t('backup.no_backup_configured')"
          :goToBackupLabel="core.$t('backup.go_to_backup')"
          :repositoryLabel="core.$t('backup.repository')"
          :statusLabel="core.$t('common.status')"
          :statusSuccessLabel="core.$t('common.success')"
          :statusNotRunLabel="core.$t('backup.backup_has_not_run_yet')"
          :statusErrorLabel="core.$t('error.error')"
          :completedLabel="core.$t('backup.completed')"
          :durationLabel="core.$t('backup.duration')"
          :totalSizeLabel="core.$t('backup.total_size')"
          :totalFileCountLabel="core.$t('backup.total_file_count')"
          :backupDisabledLabel="core.$t('common.disabled')"
          :showMoreLabel="core.$t('common.show_more')"
          :moduleId="instanceName"
          :moduleUiName="instanceLabel"
          :repositories="backupRepositories"
          :backups="backups"
          :loading="loading.listBackupRepositories || loading.listBackups"
          :coreContext="core"
          light
        />
      </cv-column>
      <cv-column :md="4" :max="4">
        <NsSystemLogsCard
          :title="core.$t('system_logs.card_title')"
          :description="
            core.$t('system_logs.card_description', {
              name: instanceLabel || instanceName,
            })
          "
          :buttonLabel="core.$t('system_logs.card_button_label')"
          :router="core.$router"
          context="module"
          :moduleId="instanceName"
          light
        />
      </cv-column>
    </cv-row>
    <!-- cluster health -->
    <template v-if="config.configured">
      <cv-row>
        <cv-column class="page-subtitle">
          <h4>{{ $t("status.cluster_health") }}</h4>
        </cv-column>
      </cv-row>
      <cv-row v-if="error.getClusterStatus">
        <cv-column>
          <NsInlineNotification
            kind="error"
            :title="$t('action.get-cluster-status')"
            :description="error.getClusterStatus"
            :showCloseButton="false"
          />
        </cv-column>
      </cv-row>
      <cv-row v-for="warning in cluster.warnings" :key="warning">
        <cv-column>
          <NsInlineNotification
            :kind="criticalWarnings.includes(warning) ? 'error' : 'warning'"
            :title="$t('status.warning_' + warning)"
            :description="$t('status.warning_' + warning + '_description')"
            :showCloseButton="false"
          />
        </cv-column>
      </cv-row>
      <cv-row>
        <cv-column :md="4" :max="4">
          <NsInfoCard
            light
            :title="cluster.leader || '-'"
            :description="$t('status.primary')"
            :icon="Star32"
            :loading="loading.getClusterStatus"
            class="min-height-card"
          />
        </cv-column>
        <cv-column :md="4" :max="4">
          <NsInfoCard
            light
            :title="
              $t('status.voters_value', {
                healthy: cluster.healthy_voters || 0,
                voters: cluster.voters || 0,
              })
            "
            :description="$t('status.quorum')"
            :icon="cluster.quorum_ok ? CheckmarkFilled32 : ErrorFilled32"
            :loading="loading.getClusterStatus"
            class="min-height-card"
          />
        </cv-column>
        <cv-column :md="4" :max="4">
          <NsInfoCard
            light
            :title="String(cluster.failure_tolerance || 0)"
            :description="$t('status.failure_tolerance')"
            :icon="Security32"
            :loading="loading.getClusterStatus"
            class="min-height-card"
          />
        </cv-column>
        <cv-column :md="4" :max="4">
          <NsInfoCard
            light
            :title="
              $t('settings.replication_' + (config.replication_mode || 'async'))
            "
            :description="$t('status.replication_mode')"
            :icon="Copy32"
            :loading="loading.getConfiguration"
            class="min-height-card"
          />
        </cv-column>
      </cv-row>
      <cv-row>
        <cv-column>
          <cv-tile light>
            <h6 class="mg-bottom-sm">{{ $t("status.postgres_members") }}</h6>
            <cv-skeleton-text
              v-if="loading.getClusterStatus"
              :paragraph="true"
              :line-count="3"
            ></cv-skeleton-text>
            <NsEmptyState
              v-else-if="!cluster.members.length"
              :title="$t('status.no_members')"
            />
            <cv-structured-list v-else>
              <template slot="headings">
                <cv-structured-list-heading>{{
                  $t("status.member")
                }}</cv-structured-list-heading>
                <cv-structured-list-heading>{{
                  $t("status.node")
                }}</cv-structured-list-heading>
                <cv-structured-list-heading>{{
                  $t("status.member_role")
                }}</cv-structured-list-heading>
                <cv-structured-list-heading>{{
                  $t("status.state")
                }}</cv-structured-list-heading>
                <cv-structured-list-heading>{{
                  $t("status.lag")
                }}</cv-structured-list-heading>
                <cv-structured-list-heading>{{
                  $t("status.timeline")
                }}</cv-structured-list-heading>
              </template>
              <template slot="items">
                <cv-structured-list-item
                  v-for="m in cluster.members"
                  :key="m.name"
                >
                  <cv-structured-list-data>{{
                    m.name
                  }}</cv-structured-list-data>
                  <cv-structured-list-data>{{
                    m.node || "-"
                  }}</cv-structured-list-data>
                  <cv-structured-list-data>
                    <NsTag
                      :kind="m.role === 'leader' ? 'green' : 'blue'"
                      :label="$t('status.pg_role_' + m.role)"
                    />
                  </cv-structured-list-data>
                  <cv-structured-list-data>{{
                    m.state
                  }}</cv-structured-list-data>
                  <cv-structured-list-data>{{
                    lagText(m)
                  }}</cv-structured-list-data>
                  <cv-structured-list-data>{{
                    m.timeline
                  }}</cv-structured-list-data>
                </cv-structured-list-item>
              </template>
            </cv-structured-list>
          </cv-tile>
        </cv-column>
      </cv-row>
      <cv-row>
        <cv-column :lg="8">
          <cv-tile light>
            <h6 class="mg-bottom-sm">{{ $t("status.etcd_members") }}</h6>
            <cv-skeleton-text
              v-if="loading.getClusterStatus"
              :paragraph="true"
              :line-count="3"
            ></cv-skeleton-text>
            <cv-structured-list v-else-if="cluster.etcd.length">
              <template slot="headings">
                <cv-structured-list-heading>{{
                  $t("status.member")
                }}</cv-structured-list-heading>
                <cv-structured-list-heading>{{
                  $t("status.vote")
                }}</cv-structured-list-heading>
                <cv-structured-list-heading>{{
                  $t("status.health")
                }}</cv-structured-list-heading>
              </template>
              <template slot="items">
                <cv-structured-list-item
                  v-for="e in cluster.etcd"
                  :key="e.name + e.peer_url"
                >
                  <cv-structured-list-data>{{
                    e.name || e.peer_url
                  }}</cv-structured-list-data>
                  <cv-structured-list-data>{{
                    e.learner ? $t("status.learner") : $t("status.voter")
                  }}</cv-structured-list-data>
                  <cv-structured-list-data>
                    <NsTag
                      :kind="e.healthy ? 'green' : 'red'"
                      :label="
                        e.healthy
                          ? $t('status.healthy')
                          : $t('status.unhealthy')
                      "
                    />
                  </cv-structured-list-data>
                </cv-structured-list-item>
              </template>
            </cv-structured-list>
            <NsEmptyState v-else :title="$t('status.no_members')" />
          </cv-tile>
        </cv-column>
        <cv-column :lg="8">
          <cv-tile light>
            <h6 class="mg-bottom-sm">{{ $t("status.databases") }}</h6>
            <cv-skeleton-text
              v-if="loading.listDatabases"
              :paragraph="true"
              :line-count="3"
            ></cv-skeleton-text>
            <NsEmptyState
              v-else-if="!databases.length"
              :title="$t('status.no_databases')"
            >
              <template #description>{{
                $t("status.no_databases_description")
              }}</template>
            </NsEmptyState>
            <cv-structured-list v-else>
              <template slot="headings">
                <cv-structured-list-heading>{{
                  $t("status.database")
                }}</cv-structured-list-heading>
                <cv-structured-list-heading>{{
                  $t("status.client")
                }}</cv-structured-list-heading>
                <cv-structured-list-heading>{{
                  $t("status.size")
                }}</cv-structured-list-heading>
                <cv-structured-list-heading>{{
                  $t("status.login")
                }}</cv-structured-list-heading>
              </template>
              <template slot="items">
                <cv-structured-list-item v-for="db in databases" :key="db.name">
                  <cv-structured-list-data>{{
                    db.name
                  }}</cv-structured-list-data>
                  <cv-structured-list-data>{{
                    (db.clients || []).join(", ") || db.client_id || "-"
                  }}</cv-structured-list-data>
                  <cv-structured-list-data>{{
                    db.size_bytes | byteFormat
                  }}</cv-structured-list-data>
                  <cv-structured-list-data>{{
                    db.owner_can_login
                      ? $t("common.enabled")
                      : $t("common.disabled")
                  }}</cv-structured-list-data>
                </cv-structured-list-item>
              </template>
            </cv-structured-list>
          </cv-tile>
        </cv-column>
      </cv-row>
    </template>
    <!-- services -->
    <cv-row>
      <cv-column class="page-subtitle">
        <h4>{{ $tc("status.services", 2) }}</h4>
      </cv-column>
    </cv-row>
    <cv-row v-if="!loading.getStatus">
      <cv-column v-if="!status.services.length">
        <cv-tile light>
          <NsEmptyState :title="$t('status.no_services')"> </NsEmptyState>
        </cv-tile>
      </cv-column>
      <cv-column
        v-else
        v-for="(service, index) in status.services"
        :key="index"
        :md="4"
        :max="4"
      >
        <NsSystemdServiceCard
          light
          class="min-height-card"
          :serviceName="service.name"
          :active="service.active"
          :failed="service.failed"
          :enabled="service.enabled"
          :icon="Cube32"
        />
      </cv-column>
    </cv-row>
    <cv-row v-else>
      <cv-column :md="4" :max="4">
        <cv-tile light>
          <cv-skeleton-text
            :paragraph="true"
            :line-count="4"
          ></cv-skeleton-text>
        </cv-tile>
      </cv-column>
    </cv-row>
    <!-- images -->
    <cv-row>
      <cv-column class="page-subtitle">
        <h4>{{ $tc("status.app_images", 2) }}</h4>
      </cv-column>
    </cv-row>
    <cv-row>
      <cv-column>
        <cv-tile light>
          <div v-if="!loading.getStatus">
            <NsEmptyState
              v-if="!status.images.length"
              :title="$t('status.no_images')"
            >
            </NsEmptyState>
            <cv-structured-list v-else>
              <template slot="headings">
                <cv-structured-list-heading>{{
                  $t("status.name")
                }}</cv-structured-list-heading>
                <cv-structured-list-heading>{{
                  $t("status.size")
                }}</cv-structured-list-heading>
                <cv-structured-list-heading>{{
                  $t("status.created")
                }}</cv-structured-list-heading>
              </template>
              <template slot="items">
                <cv-structured-list-item
                  v-for="(image, index) in status.images"
                  :key="index"
                >
                  <cv-structured-list-data class="break-word">{{
                    image.name
                  }}</cv-structured-list-data>
                  <cv-structured-list-data>{{
                    image.size
                  }}</cv-structured-list-data>
                  <cv-structured-list-data class="break-word">{{
                    image.created
                  }}</cv-structured-list-data>
                </cv-structured-list-item>
              </template>
            </cv-structured-list>
          </div>
          <cv-skeleton-text
            v-else
            :paragraph="true"
            :line-count="5"
          ></cv-skeleton-text>
        </cv-tile>
      </cv-column>
    </cv-row>
    <!-- volumes -->
    <cv-row>
      <cv-column class="page-subtitle">
        <h4>{{ $tc("status.app_volumes", 2) }}</h4>
      </cv-column>
    </cv-row>
    <cv-row>
      <cv-column>
        <cv-tile light>
          <div v-if="!loading.getStatus">
            <NsEmptyState
              v-if="!status.volumes.length"
              :title="$t('status.no_volumes')"
            >
            </NsEmptyState>
            <cv-structured-list v-else>
              <template slot="headings">
                <cv-structured-list-heading>{{
                  $t("status.name")
                }}</cv-structured-list-heading>
                <cv-structured-list-heading>{{
                  $t("status.mount")
                }}</cv-structured-list-heading>
                <cv-structured-list-heading>{{
                  $t("status.created")
                }}</cv-structured-list-heading>
              </template>
              <template slot="items">
                <cv-structured-list-item
                  v-for="(volume, index) in status.volumes"
                  :key="index"
                >
                  <cv-structured-list-data>{{
                    volume.name
                  }}</cv-structured-list-data>
                  <cv-structured-list-data class="break-word">{{
                    volume.mount
                  }}</cv-structured-list-data>
                  <cv-structured-list-data>{{
                    volume.created
                  }}</cv-structured-list-data>
                </cv-structured-list-item>
              </template>
            </cv-structured-list>
          </div>
          <cv-skeleton-text
            v-else
            :paragraph="true"
            :line-count="5"
          ></cv-skeleton-text>
        </cv-tile>
      </cv-column>
    </cv-row>
  </cv-grid>
</template>

<script>
import to from "await-to-js";
import { mapState } from "vuex";
import {
  QueryParamService,
  TaskService,
  IconService,
  UtilService,
  PageTitleService,
} from "@nethserver/ns8-ui-lib";
import ModuleTask from "@/mixins/moduleTask";
import DataBase32 from "@carbon/icons-vue/es/data--base/32";
import Network_232 from "@carbon/icons-vue/es/network--2/32";
import Star32 from "@carbon/icons-vue/es/star/32";
import Security32 from "@carbon/icons-vue/es/security/32";
import Copy32 from "@carbon/icons-vue/es/copy/32";
import CheckmarkFilled32 from "@carbon/icons-vue/es/checkmark--filled/32";
import ErrorFilled32 from "@carbon/icons-vue/es/error--filled/32";

export default {
  name: "Status",
  mixins: [
    ModuleTask,
    TaskService,
    QueryParamService,
    IconService,
    UtilService,
    PageTitleService,
  ],
  pageTitle() {
    return this.$t("status.title") + " - " + this.appName;
  },
  data() {
    return {
      q: {
        page: "status",
      },
      urlCheckInterval: null,
      isRedirectChecked: false,
      redirectTimeout: 0,
      host: "",
      DataBase32,
      Network_232,
      Star32,
      Security32,
      Copy32,
      CheckmarkFilled32,
      ErrorFilled32,
      criticalWarnings: ["quorum_lost", "no_leader", "etcd_unreachable"],
      config: { configured: false, role: "", endpoint: {} },
      cluster: { members: [], etcd: [], warnings: [] },
      databases: [],
      refreshInterval: null,
      status: {
        instance: "",
        services: [],
        images: [],
        volumes: [],
      },
      backupRepositories: [],
      backups: [],
      loading: {
        getStatus: false,
        listBackupRepositories: false,
        listBackups: false,
        getConfiguration: false,
        getClusterStatus: false,
        listDatabases: false,
      },
      error: {
        getConfiguration: "",
        getClusterStatus: "",
        getStatus: "",
        listBackupRepositories: "",
        listBackups: "",
      },
    };
  },
  computed: {
    ...mapState(["instanceName", "instanceLabel", "core", "appName"]),
    endpointTitle() {
      const e = this.config.endpoint || {};
      return e.host && e.port ? `${e.host}:${e.port}` : "-";
    },
    installationNodeTitle() {
      if (this.status && this.status.node) {
        if (this.status.node_ui_name) {
          return this.status.node_ui_name;
        } else {
          return this.$t("status.node") + " " + this.status.node;
        }
      } else {
        return "-";
      }
    },
    installationNodeTitleTooltip() {
      if (this.status && this.status.node_ui_name) {
        return this.$t("status.node") + " " + this.status.node;
      } else {
        return "";
      }
    },
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
  mounted() {
    this.redirectTimeout = setTimeout(
      () => (this.isRedirectChecked = true),
      200
    );
  },
  beforeUnmount() {
    clearTimeout(this.redirectTimeout);
  },
  beforeDestroy() {
    clearInterval(this.refreshInterval);
  },
  created() {
    this.getConfiguration();
    this.getStatus();
    this.listBackupRepositories();
  },
  methods: {
    lagText(m) {
      if (m.role === "leader") {
        return "-";
      }
      if (m.lag_bytes < 0) {
        return this.$t("status.unknown");
      }
      return this.$options.filters.byteFormat(m.lag_bytes);
    },
    goToPgadmin() {
      window.open(`https://${this.config.host}`, "_blank");
    },
    async getClusterStatus() {
      this.error.getClusterStatus = "";
      try {
        const out = await this.runModuleTask("get-cluster-status");
        this.cluster = {
          members: [],
          etcd: [],
          warnings: [],
          ...out,
        };
      } catch (failure) {
        this.error.getClusterStatus = this.taskErrorMessage(failure);
      }
      this.loading.getClusterStatus = false;
    },
    async listDatabases() {
      try {
        const out = await this.runModuleTask("list-databases");
        this.databases = out.databases || [];
      } catch (failure) {
        this.databases = [];
      }
      this.loading.listDatabases = false;
    },
    refreshCluster() {
      if (this.config.configured) {
        this.getClusterStatus();
      }
    },
    async getConfiguration() {
      this.loading.getConfiguration = true;
      this.error.getConfiguration = "";
      const taskAction = "get-configuration";
      const eventId = this.getUuid();

      // register to task error
      this.core.$root.$once(
        `${taskAction}-aborted-${eventId}`,
        this.getConfigurationAborted
      );

      // register to task completion
      this.core.$root.$once(
        `${taskAction}-completed-${eventId}`,
        this.getConfigurationCompleted
      );

      const res = await to(
        this.createModuleTaskForApp(this.instanceName, {
          action: taskAction,
          extra: {
            title: this.$t("action." + taskAction),
            isNotificationHidden: true,
            eventId,
          },
        })
      );
      const err = res[0];

      if (err) {
        console.error(`error creating task ${taskAction}`, err);
        this.error.getConfiguration = this.getErrorMessage(err);
        this.loading.getConfiguration = false;
        return;
      }
    },
    getConfigurationAborted(taskResult, taskContext) {
      console.error(`${taskContext.action} aborted`, taskResult);
      this.error.getConfiguration = this.$t("error.generic_error");
      this.loading.getConfiguration = false;
    },
    getConfigurationCompleted(taskContext, taskResult) {
      const config = taskResult.output;
      this.config = config;
      this.host = config.host;
      this.loading.getConfiguration = false;
      if (config.configured) {
        this.loading.getClusterStatus = true;
        this.loading.listDatabases = true;
        this.getClusterStatus();
        this.listDatabases();
        clearInterval(this.refreshInterval);
        this.refreshInterval = setInterval(this.refreshCluster, 15000);
      }
    },
    async getStatus() {
      this.loading.getStatus = true;
      this.error.getStatus = "";
      const taskAction = "get-status";
      const eventId = this.getUuid();

      // register to task error
      this.core.$root.$once(
        `${taskAction}-aborted-${eventId}`,
        this.getStatusAborted
      );

      // register to task completion
      this.core.$root.$once(
        `${taskAction}-completed-${eventId}`,
        this.getStatusCompleted
      );

      const res = await to(
        this.createModuleTaskForApp(this.instanceName, {
          action: taskAction,
          extra: {
            title: this.$t("action." + taskAction),
            isNotificationHidden: true,
            eventId,
          },
        })
      );
      const err = res[0];

      if (err) {
        console.error(`error creating task ${taskAction}`, err);
        this.error.getStatus = this.getErrorMessage(err);
        this.loading.getStatus = false;
        return;
      }
    },
    getStatusAborted(taskResult, taskContext) {
      console.error(`${taskContext.action} aborted`, taskResult);
      this.error.getStatus = this.$t("error.generic_error");
      this.loading.getStatus = false;
    },
    getStatusCompleted(taskContext, taskResult) {
      this.status = taskResult.output;
      this.loading.getStatus = false;
    },
    async listBackupRepositories() {
      this.loading.listBackupRepositories = true;
      this.error.listBackupRepositories = "";
      const taskAction = "list-backup-repositories";
      const eventId = this.getUuid();

      // register to task error
      this.core.$root.$once(
        `${taskAction}-aborted-${eventId}`,
        this.listBackupRepositoriesAborted
      );

      // register to task completion
      this.core.$root.$once(
        `${taskAction}-completed-${eventId}`,
        this.listBackupRepositoriesCompleted
      );

      const res = await to(
        this.createClusterTaskForApp({
          action: taskAction,
          extra: {
            title: this.$t("action." + taskAction),
            isNotificationHidden: true,
            eventId,
          },
        })
      );
      const err = res[0];

      if (err) {
        console.error(`error creating task ${taskAction}`, err);
        this.error.listBackupRepositories = this.getErrorMessage(err);
        this.loading.listBackupRepositories = false;
        return;
      }
    },
    listBackupRepositoriesAborted(taskResult, taskContext) {
      console.error(`${taskContext.action} aborted`, taskResult);
      this.error.listBackupRepositories = this.$t("error.generic_error");
      this.loading.listBackupRepositories = false;
    },
    listBackupRepositoriesCompleted(taskContext, taskResult) {
      let backupRepositories = taskResult.output.repositories.sort(
        this.sortByProperty("name")
      );
      this.backupRepositories = backupRepositories;
      this.loading.listBackupRepositories = false;
      this.listBackups();
    },
    async listBackups() {
      this.loading.listBackups = true;
      this.error.listBackups = "";
      const taskAction = "list-backups";
      const eventId = this.getUuid();

      // register to task error
      this.core.$root.$once(
        `${taskAction}-aborted-${eventId}`,
        this.listBackupsAborted
      );

      // register to task completion
      this.core.$root.$once(
        `${taskAction}-completed-${eventId}`,
        this.listBackupsCompleted
      );

      const res = await to(
        this.createClusterTaskForApp({
          action: taskAction,
          extra: {
            title: this.$t("action." + taskAction),
            isNotificationHidden: true,
            eventId,
          },
        })
      );
      const err = res[0];

      if (err) {
        console.error(`error creating task ${taskAction}`, err);
        this.error.listBackups = this.getErrorMessage(err);
        this.loading.listBackups = false;
        return;
      }
    },
    listBackupsAborted(taskResult, taskContext) {
      console.error(`${taskContext.action} aborted`, taskResult);
      this.error.listBackups = this.$t("error.generic_error");
      this.loading.listBackups = false;
    },
    listBackupsCompleted(taskContext, taskResult) {
      let backups = taskResult.output.backups;
      backups.sort(this.sortByProperty("name"));

      // get repository name
      for (const backup of backups) {
        const repo = this.backupRepositories.find(
          (r) => r.id == backup.repository
        );

        if (repo) {
          backup.repoName = repo.name;
        }
      }
      this.backups = backups;
      this.loading.listBackups = false;
    },
  },
};
</script>

<style scoped lang="scss">
@import "../styles/carbon-utils";

.mg-bottom-sm {
  margin-bottom: $spacing-05;
}

.break-word {
  word-wrap: break-word;
  max-width: 30vw;
}
</style>
