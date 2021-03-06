from kubernetes import client, config
from kubernetes.client.rest import ApiException
# FIXME(typhoonzero): still need to import settings
from django.conf import settings

import copy
import os
import logging
import traceback

import utils
import volume

# FIXME(typhoonzero): need a base class to define the interfaces?
class K8sProvider:
    """
        Kubernetes Cloud Porvider
        Provide interfaces for manage jobs and resources.
    """
    def __init__(self):
        pass
    
    def get_jobs(self, username):
        namespace = utils.email_escape(username)
        api_instance =\
            client.BatchV1Api(api_client=utils.get_user_api_client(username))
        job_list = api_instance.list_namespaced_job(namespace)
        # NOTE: when job is deleted, some pods of the job will be at "Terminating" status
        # for a while, which may cause submit fail. Find all pods that are still "Terminating".
        user_pod_list =\
            client.CoreV1Api(api_client=utils.get_user_api_client(username))\
            .list_namespaced_pod(namespace)
        terminating_jobs = []
        for pod in user_pod_list.items:
            jobname = ""
            if not pod.metadata.labels:
                continue
            if "paddle-job" in pod.metadata.labels:
                jobname = pod.metadata.labels["paddle-job"]
            elif "paddle-job-master" in pod.metadata.labels:
                jobname = pod.metadata.labels["paddle-job-master"]
            elif "paddle-job-pserver" in pod.metadata.labels:
                jobname = pod.metadata.labels["paddle-job-pserver"]
            if pod.metadata.deletion_timestamp and jobname:
                if jobname not in terminating_jobs:
                    terminating_jobs.append(jobname)
        # NOTE: put it in the original dict for backward compability
        ret_dict = copy.deepcopy(job_list.to_dict())
        ret_dict["terminating"] = terminating_jobs
        return ret_dict

    def __setup_volumes(self, paddlejob, username):
        volumes = []
        for k, cfg in settings.DATACENTERS.items():
            if k != paddlejob.dc and k != "public":
                continue
            fstype = cfg["fstype"]
            if fstype == settings.FSTYPE_CEPHFS:
                if k == "public":
                    mount_path = cfg["mount_path"] % paddlejob.dc
                    cephfs_path = cfg["cephfs_path"]
                else:
                    mount_path = cfg["mount_path"] % (paddlejob.dc, username)
                    cephfs_path = cfg["cephfs_path"] % username
                volumes.append(volume.get_volume_config(
                    fstype = fstype,
                    name = k.replace("_", "-"),
                    monitors_addr = cfg["monitors_addr"],
                    secret = cfg["secret"],
                    user = cfg["user"],
                    mount_path = mount_path,
                    cephfs_path = cephfs_path,
                    admin_key = cfg["admin_key"],
                    read_only = cfg.get("read_only", False)
                ))
            elif fstype == settings.FSTYPE_HOSTPATH:
                if k == "public":
                    mount_path = cfg["mount_path"] % paddlejob.dc
                    host_path = cfg["host_path"]
                else:
                    mount_path = cfg["mount_path"] % (paddlejob.dc, username)
                    host_path = cfg["host_path"] % username

                volumes.append(volume.get_volume_config(
                    fstype = fstype,
                    name = k.replace("_", "-"),
                    mount_path = mount_path,
                    host_path = host_path
                ))
            else:
                pass
        paddlejob.volumes = volumes

    def submit_job(self, paddlejob, username):
        namespace = utils.email_escape(username)
        api_client = utils.get_user_api_client(username)
        self.__setup_volumes(paddlejob, username)
        if not paddlejob.registry_secret:
            paddlejob.registry_secret = settings.JOB_DOCKER_IMAGE.get("registry_secret", None)
        if not paddlejob.image:
            if paddlejob.gpu > 0:
                paddlejob.image = settings.JOB_DOCKER_IMAGE["image_gpu"]
            else:
                paddlejob.image = settings.JOB_DOCKER_IMAGE["image"]
        # jobPackage validation: startwith /pfs
        # NOTE: job packages are uploaded to /pfs/[dc]/home/[user]/jobs/[jobname]
        package_in_pod = os.path.join("/pfs/%s/home/%s"%(paddlejob.dc, username), "jobs", paddlejob.name)

        logging.info("current package: %s", package_in_pod)
        # package must be ready before submit a job
        current_package_path = package_in_pod.replace("/pfs/%s/home"%paddlejob.dc, settings.STORAGE_PATH)
        if not os.path.exists(current_package_path):
            current_package_path = package_in_pod.replace("/pfs/%s/home/%s"%(paddlejob.dc, username), settings.STORAGE_PATH)
            if not os.path.exists(current_package_path):
                raise Exception("package not exist in cloud: %s"%current_package_path)
        logging.info("current package in pod: %s", current_package_path)
        # GPU quota management
        # TODO(Yancey1989) We should move this to Kubernetes
        if 'GPU_QUOTA' in dir(settings) and int(paddlejob.gpu) > 0:
            gpu_usage = 0
            pods = client.CoreV1Api(api_client=api_client).list_namespaced_pod(namespace=namespace)
            for pod in pods.items:
                # only statistics trainer GPU resource, pserver does not use GPU
                if pod.metadata.labels and 'paddle-job' in pod.metadata.labels and \
                    pod.status.phase == 'Running':
                    gpu_usage += int(pod.spec.containers[0].resources.limits.get('alpha.kubernetes.io/nvidia-gpu', '0'))
            if username in settings.GPU_QUOTA:
                gpu_quota = settings.GPU_QUOTA[username]['limit']
            else:
                gpu_quota = settings.GPU_QUOTA['DEFAULT']['limit']
            gpu_available = gpu_quota - gpu_usage
            gpu_request = int(paddlejob.gpu) * int(paddlejob.parallelism)
            logging.info('gpu available: %d, gpu request: %d' % (gpu_available, gpu_request))
            if gpu_available < gpu_request:
                raise Exception("You don't have enought GPU quota," + \
                    "request: %d, usage: %d, limit: %d" % (gpu_request, gpu_usage, gpu_quota))

        # add Nvidia lib volume if training with GPU
        if paddlejob.gpu > 0:
            paddlejob.volumes.append(volume.get_volume_config(
                fstype = settings.FSTYPE_HOSTPATH,
                name = "nvidia-libs",
                mount_path = "/usr/local/nvidia/lib64",
                host_path = settings.NVIDIA_LIB_PATH
            ))
        # ========== submit master ReplicaSet if using fault_tolerant feature ==
        # FIXME: alpha features in separate module
        if paddlejob.fault_tolerant:
            try:
                ret = client.ExtensionsV1beta1Api(api_client=api_client).create_namespaced_replica_set(
                    namespace,
                    paddlejob.new_master_job())
            except ApiException, e:
                logging.error("error submitting master job: %s", traceback.format_exc())
                raise e
        # ========================= submit pserver job =========================
        try:
            ret = client.ExtensionsV1beta1Api(api_client=api_client).create_namespaced_replica_set(
                namespace,
                paddlejob.new_pserver_job())
        except ApiException, e:
            logging.error("error submitting pserver job: %s ", traceback.format_exc())
            raise e
        # ========================= submit trainer job =========================
        try:
            ret = client.BatchV1Api(api_client=api_client).create_namespaced_job(
                namespace,
                paddlejob.new_trainer_job())
        except ApiException, e:
            logging.error("error submitting trainer job: %s" % traceback.format_exc())
            raise e
        return ret

    def delete_job(self, jobname, username):
        namespace = utils.email_escape(username)
        api_client = utils.get_user_api_client(username)
        if not jobname:
            return utils.simple_response(500, "must specify jobname")
        # FIXME: options needed: grace_period_seconds, orphan_dependents, preconditions
        # FIXME: cascade delteing
        delete_status = []
        # delete job
        trainer_name = jobname + "-trainer"
        try:
            u_status = client.BatchV1Api(api_client=api_client)\
                .delete_namespaced_job(trainer_name, namespace, {})
        except ApiException, e:
            logging.error("error deleting job: %s, %s", jobname, str(e))
            delete_status.append(str(e))

        # delete job pods
        try:
            job_pod_list = client.CoreV1Api(api_client=api_client)\
                .list_namespaced_pod(namespace,
                                     label_selector="paddle-job=%s"%jobname)
            for i in job_pod_list.items:
                u_status = client.CoreV1Api(api_client=api_client)\
                    .delete_namespaced_pod(i.metadata.name, namespace, {})
        except ApiException, e:
            logging.error("error deleting job pod: %s", str(e))
            delete_status.append(str(e))

        # delete pserver rs
        pserver_name = jobname + "-pserver"
        try:
            u_status = client.ExtensionsV1beta1Api(api_client=api_client)\
                .delete_namespaced_replica_set(pserver_name, namespace, {})
        except ApiException, e:
            logging.error("error deleting pserver: %s" % str(e))
            delete_status.append(str(e))

        # delete pserver pods
        try:
            # pserver replica set has label with jobname
            job_pod_list = client.CoreV1Api(api_client=api_client)\
                .list_namespaced_pod(namespace,
                                     label_selector="paddle-job-pserver=%s"%jobname)
            for i in job_pod_list.items:
                u_status = client.CoreV1Api(api_client=api_client)\
                    .delete_namespaced_pod(i.metadata.name, namespace, {})
        except ApiException, e:
            logging.error("error deleting pserver pods: %s" % str(e))
            delete_status.append(str(e))

        # delete master rs
        master_name = jobname + "-master"
        try:
            u_status = client.ExtensionsV1beta1Api(api_client=api_client)\
                .delete_namespaced_replica_set(master_name, namespace, {})
        except ApiException, e:
            logging.error("error deleting master: %s" % str(e))
            # just ignore deleting master failed, we do not set up master process
            # without fault tolerant mode
            #delete_status.append(str(e))

        # delete master pods
        try:
            # master replica set has label with jobname
            job_pod_list = client.CoreV1Api(api_client=api_client)\
                .list_namespaced_pod(namespace,
                                     label_selector="paddle-job-master=%s"%jobname)
            for i in job_pod_list.items:
                u_status = client.CoreV1Api(api_client=api_client)\
                    .delete_namespaced_pod(i.metadata.name, namespace, {})
        except ApiException, e:
            logging.error("error deleting master pods: %s" % str(e))
            # just ignore deleting master failed, we do not set up master process
            # without fault tolerant mode
            #delete_status.append(str(e))

        if len(delete_status) > 0:
            retcode = 500
        else:
            retcode = 200
        return retcode, delete_status

    def get_pservers(self, username):
        namespace = utils.email_escape(username)
        api_instance = client.ExtensionsV1beta1Api(api_client=utils.get_user_api_client(username))
        return api_instance.list_namespaced_replica_set(namespace).to_dict()

    def get_logs(self, jobname, num_lines, worker, username):
        def _get_pod_log(api_client, namespace, pod_name, num_lines):
            try:
                if num_lines:
                    pod_log = client.CoreV1Api(api_client=api_client)\
                        .read_namespaced_pod_log(
                            pod_name, namespace, tail_lines=int(num_lines))
                else:
                    pod_log = client.CoreV1Api(api_client=api_client)\
                        .read_namespaced_pod_log(i.metadata.name, namespace)
                return pod_log
            except ApiException, e:
                return str(e)

        namespace = utils.email_escape(username)
        api_client = utils.get_user_api_client(username)
        job_pod_list = client.CoreV1Api(api_client=api_client)\
            .list_namespaced_pod(namespace, label_selector="paddle-job=%s"%jobname)
        total_job_log = ""
        if not worker:
            for i in job_pod_list.items:
                total_job_log = "".join((total_job_log, 
                    "==========================%s==========================" % i.metadata.name))
                pod_log = _get_pod_log(api_client, namespace, i.metadata.name, num_lines)
                total_job_log = "\n".join((total_job_log, pod_log))
        else:
            total_job_log = _get_pod_log(api_client, namespace, worker, num_lines)
        return total_job_log

    def get_workers(self, jobname, username):
        namespace = utils.email_escape(username)
        job_pod_list = None
        api_client = utils.get_user_api_client(username)
        if not jobname:
            job_pod_list = client.CoreV1Api(api_client=api_client)\
                .list_namespaced_pod(namespace)
        else:
            selector = "paddle-job=%s"%jobname
            job_pod_list = client.CoreV1Api(api_client=api_client)\
                .list_namespaced_pod(namespace, label_selector=selector)
        return job_pod_list.to_dict()
    
    def get_quotas(self, username):
        namespace = utils.email_escape(username)
        api_client = utils.get_user_api_client(username)
        quota_list = client.CoreV1Api(api_client=api_client)\
            .list_namespaced_resource_quota(namespace)
        return quota_list.to_dict()