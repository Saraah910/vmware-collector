data "vcd_resource_list" "list_of_vms_by_name" {
  name = "list_of_vms_by_name"
  resource_type = "vcd_all_vm"
  list_mode = "name"
  org = var.vcd_org
  vdc = var.vcd_vdc
}
data "vcd_vm" "vm_details" {
  for_each = toset(data.vcd_resource_list.list_of_vms_by_name.list)
  name = each.value
  org = var.vcd_org
  vdc = var.vcd_vdc
}
data "vcd_resource_list" "list_of_independent_disks_by_name" {
  name = "list_of_independent_disks_by_name"
  resource_type = "vcd_independent_disk"
  list_mode = "name"
  org = var.vcd_org
  vdc = var.vcd_vdc
}
data "vcd_independent_disk" "independent_disks_details" {
  for_each = toset(data.vcd_resource_list.list_of_independent_disks_by_name.list)
  name = each.value
  org = var.vcd_org
  vdc = var.vcd_vdc
}
data "vcd_vm" "vm_names_by_id_attached_to_independent_disks" {
  for_each = local.attached_vm_ids
  name = each.key
  org = var.vcd_org
  vdc = var.vcd_vdc
}

locals {
    attached_vm_ids = toset(flatten([
        for disk in data.vcd_independent_disk.independent_disks_details :
            disk.attached_vm_ids
    ]))
    attached_vms_by_name = {
        for vm_id in distinct(flatten([
            for disk in data.vcd_independent_disk.independent_disks_details :
                disk.attached_vm_ids
            ])) :
            data.vcd_vm.vm_names_by_id_attached_to_independent_disks[vm_id].name => [
            for disk in data.vcd_independent_disk.independent_disks_details :
                disk.name if contains(disk.attached_vm_ids, vm_id)
        ]
    }
    vm_summary = {
        for vm_name, vm in data.vcd_vm.vm_details : vm_name => {
        vm_name      = vm.name
        configured_os = vm.os_type
        guest_os = try(
            regex("prettyName='([^']+)'", join("", [
            for e in vm.extra_config : e.value
                if e.key == "guestInfo.detailed.data"
            ]))[0],
            "Unknown"
        )
        vmware_tools = {
            status = length([for e in vm.extra_config : e if e.key == "vmware.tools.internalversion"]) > 0 ? "Installed" : "Not Installed"
            update_required = (try(tonumber(one([for e in vm.extra_config : e.value if e.key == "vmware.tools.internalversion"])),0) < 
            try(tonumber(one([for e in vm.extra_config : e.value if e.key == "vmware.tools.requiredversion"])),0)) ? "Yes" : "No"
        }
        networks = [for net in vm.network: {
            name = net.name,
            ip = net.ip,
            connected = net.connected
        }]
        cpus   = vm.cpus
        cores = vm.cpu_cores
        # cpu_hot_add_enabled = vm.cpu_hot_add_enabled
        memory = (vm.memory/1024)
        # memory_hot_add_enabled = vm.memory_hot_add_enabled
        memory_limit = (vm.memory_limit/1024)
        virtual_disks = [for vdisk in vm.internal_disk: {
            bus_type = vdisk.bus_type,
            storage_profile = vdisk.storage_profile,
            size_in_gb = (vdisk.size_in_mb/1024),
            thin_provisioned = vdisk.thin_provisioned
        }]
        independant_disks = try([for vm_name, disks_list in local.attached_vms_by_name : disks_list if vm_name == vm.name],null)
        # hardware_bindings = {
        #     sizing_policy_id    = vm.sizing_policy_id
        #     placement_policy_id = vm.placement_policy_id
        # }
        }
    }
}
output "vms" {
  value = local.vm_summary
}

resource "local_file" "vm_json" {
  content  = jsonencode(local.vm_summary)
  filename = "./output/vm_details.json"
}
resource "terraform_data" "generate_xls" {
  depends_on = [local_file.vm_json]
  triggers_replace = {
    vm_hash  = md5(local_file.vm_json.content)
  }
  provisioner "local-exec" {
    command = "python ${path.module}/report.py ./output/vm_details.json"
  }
}

