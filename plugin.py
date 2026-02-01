import gradio as gr
from shared.utils.plugins import WAN2GPPlugin
import os
import json
import tempfile
import html
import copy
import sys
import time
import inspect
from PIL import Image
import re
import io
import base64
import glob

class QueueManagerPlugin(WAN2GPPlugin):
    def __init__(self):
        super().__init__()
        self.qm_apply_btn = None
        self.qm_cancel_btn = None
        self.js_trigger_index = None
        self.main_state = None
        self.main_tabs = None
        self.live_queue_html = None
        self.main_edit_btn = None
        self.main_cancel_btn = None
        self.qm_buttons_row = None

        self.ordered_input_keys = []
        self.ordered_input_components = []
        self.captured_data = {}
        self.all_known_inputs = []

    def setup_ui(self):
        self.add_tab(
            tab_id="queue_manager_tab",
            label="Queue Editor",
            component_constructor=self.create_ui,
            position=2
        )

        self.request_global("_parse_queue_zip")
        self.request_global("_save_queue_to_zip")
        self.request_global("get_gen_info")
        self.request_global("update_queue_data")
        self.request_global("global_queue_ref")
        self.request_global("update_loras_url_cache")
        self.request_global("get_lora_dir")
        self.request_global("save_inputs")

        self.request_global("get_video_frame")
        self.request_global("get_video_info")
        self.request_global("extract_source_images")
        self.request_global("has_image_file_extension")
        self.request_global("has_video_file_extension")

        self.request_component("state")
        self.request_component("main_tabs")
        self.request_component("edit_btn")
        self.request_component("cancel_btn")
        self.request_component("queue_action_input")
        self.request_component("queue_html")
        self.request_component("js_trigger_index")

        try:
            main_module = sys.modules['__main__']
            target_func = getattr(main_module, 'save_inputs', None)
            
            if target_func and callable(target_func):
                sig = inspect.signature(target_func)
                excluded_args = ['target', 'state', 'plugin_data', 'image_mask_guide']
                
                self.all_known_inputs = [
                    param.name for param in sig.parameters.values()
                    if param.name not in excluded_args
                ]
        except Exception:
            pass

        for name in self.all_known_inputs:
            self.request_component(name)

        js = """
        window.qmHandleAction = function(action, param) {
            const inputContainer = document.getElementById("qm_action_input");
            const btnContainer = document.getElementById("qm_action_trigger");
            
            if (!inputContainer || !btnContainer) return;
            
            const textarea = inputContainer.querySelector("textarea");
            if (!textarea) return;

            textarea.value = JSON.stringify({action: action, param: param});
            textarea.dispatchEvent(new Event("input", { bubbles: true }));
            btnContainer.click(); 
        };
        
        window.qmRowClick = function(e, index) {
            if (window.getSelection().toString().length > 0) return;
            window.qmHandleAction('select', index);
        };

        window.qmDragStart = function(e) {
            window.qmDragSrcRow = e.currentTarget;
            e.dataTransfer.effectAllowed = 'move';
            e.dataTransfer.setData('text/html', e.currentTarget.innerHTML);
            e.currentTarget.classList.add('drag-active');
        };

        window.qmDragOver = function(e) {
            if (window.qmDragSrcRow) {
                e.preventDefault();
                e.dataTransfer.dropEffect = 'move';
                if (e.currentTarget === window.qmDragSrcRow) return;
                e.currentTarget.classList.add('drag-over');
            }
        };
        
        window.qmDragLeave = function(e) {
            if (window.qmDragSrcRow && e.currentTarget !== window.qmDragSrcRow) {
                e.currentTarget.classList.remove('drag-over');
            }
        };

        window.qmDrop = function(e) {
            e.stopPropagation();
            e.preventDefault();
            
            const targetRow = e.currentTarget;
            const srcRow = window.qmDragSrcRow;
            
            document.querySelectorAll('.draggable-row').forEach(row => {
                row.classList.remove('drag-over', 'drag-active');
            });
            
            if (srcRow && srcRow !== targetRow) {
                const fromIndex = parseInt(srcRow.dataset.index);
                const toIndex = parseInt(targetRow.dataset.index);
                window.qmHandleAction('move', [fromIndex, toIndex]);
            }
            window.qmDragSrcRow = null;
            return false;
        };
        
        window.qmDragEnd = function(e) {
            document.querySelectorAll('.draggable-row').forEach(row => {
                row.classList.remove('drag-over', 'drag-active');
            });
            window.qmDragSrcRow = null;
        };
        """
        self.add_custom_js(js)

    def post_ui_setup(self, components):
        self.main_edit_btn = components.get("edit_btn")
        self.main_cancel_btn = components.get("cancel_btn")
        self.main_state = components.get("state")
        self.main_tabs = components.get("main_tabs")
        self.live_queue_html = components.get("queue_html")
        self.js_trigger_index = components.get("js_trigger_index")

        self.ordered_input_keys = []
        self.ordered_input_components = []

        for name in self.all_known_inputs:
            comp = components.get(name)
            if comp:
                self.ordered_input_keys.append(name)
                self.ordered_input_components.append(comp)
        
        if self.main_edit_btn:
            self.insert_after("edit_btn", self.create_qm_buttons)

    def create_qm_buttons(self):
        with gr.Row(visible=False) as self.qm_buttons_row:
            self.qm_apply_btn = gr.Button("Apply Edits", variant="primary", scale=1, elem_id="qm_apply_btn")
            self.qm_cancel_btn = gr.Button("Cancel", scale=1)
            
        inputs_list = [self.main_state] + self.ordered_input_components

        self.qm_apply_btn.click(
            fn=self.prepare_apply,
            inputs=inputs_list,
            outputs=[self.main_state, self.js_trigger_index]
        ).then(
            fn=None,
            js="""() => { 
                const silentBtn = document.getElementById('silent_edit_tab_cancel_button');
                const cancelBtn = document.getElementById('edit_tab_cancel_button');
                if (silentBtn && silentBtn.offsetParent !== null) {
                    silentBtn.click();
                } else if (cancelBtn) {
                    cancelBtn.click();
                }
            }"""
        )
        
        return self.qm_buttons_row

    def _sanitize_captured_data(self, data):
        """Cleans Gradio gallery/tuple outputs into simple paths/objects."""
        cleaned = {}
        for k, v in data.items():
            if k in ['image_start', 'image_end', 'image_refs'] and isinstance(v, list):
                new_v = []
                for item in v:
                    if isinstance(item, (list, tuple)) and len(item) > 0:
                        new_v.append(item[0])
                    else:
                        new_v.append(item)
                cleaned[k] = new_v
            else:
                cleaned[k] = v
        return cleaned

    def prepare_apply(self, state, *args):
        direct_data = {}
        input_values = args 
        
        for i, key in enumerate(self.ordered_input_keys):
            if i < len(input_values):
                val = input_values[i]
                if key == 'loras_choices':
                    direct_data['activated_loras'] = val
                else:
                    direct_data[key] = val

        self.captured_data = self._sanitize_captured_data(direct_data)
        state["qm_intercept"] = True
        return state, str(time.time())

    def post_apply_handler(self, state, queue, index_being_edited):
        intercept = state.get("qm_intercept", False)
        if not intercept:
            return [gr.update()] * 6
            
        state["qm_intercept"] = False
        state["editing_task_id"] = None
        
        if index_being_edited is None or index_being_edited < 0:
            return gr.Tabs(selected="plugin_queue_manager_tab"), queue, gr.update(), -1, gr.update(), False
            
        captured = self.captured_data
        
        if captured and 0 <= index_being_edited < len(queue):
            orig_task = queue[index_being_edited]

            if 'activated_loras' in captured and self.update_loras_url_cache and self.get_lora_dir:
                model_type = orig_task['params'].get('model_type')
                if model_type:
                    lora_dir = self.get_lora_dir(model_type)
                    captured['activated_loras'] = self.update_loras_url_cache(lora_dir, captured['activated_loras'])

            new_params = orig_task.get('params', {}).copy()
            new_params.update(captured)

            orig_task['params'] = new_params
            orig_task['prompt'] = new_params.get('prompt', orig_task.get('prompt'))
            orig_task['steps'] = new_params.get('num_inference_steps', orig_task.get('steps'))
            orig_task['length'] = new_params.get('video_length', orig_task.get('length'))
            orig_task['repeats'] = new_params.get('repeat_generation', orig_task.get('repeats', 1))
            
            queue[index_being_edited] = orig_task
            gr.Info("Queue Manager: Task updated.")

        gen = self.get_gen_info(state)
        live_queue = gen.get("queue", [])

        clean_queue = [t for t in live_queue if t.get('id', 0) >= -999]
        gen["queue"] = clean_queue

        self.update_queue_data(clean_queue)
        
        html_update = self.generate_table_html(queue)
        live_queue_html = self.update_queue_data(gen["queue"])
        
        return gr.Tabs(selected="plugin_queue_manager_tab"), queue, html_update, -1, live_queue_html, False

    def create_ui(self):
        css = """
        .qm-table { width: 100%; border-collapse: collapse; margin-bottom: 20px; table-layout: fixed; }
        .qm-table th { background-color: var(--table-even-background-fill); font-weight: bold; padding: 8px; border: 1px solid var(--border-color-primary); }
        .qm-table td { padding: 8px; border: 1px solid var(--border-color-primary); vertical-align: middle; word-wrap: break-word; }
        .center-align { text-align: center; }
        .text-left { text-align: left; }
        .prompt-cell {
            max-height: 4.8em; overflow: hidden; text-overflow: ellipsis; display: -webkit-box;
            -webkit-line-clamp: 3; -webkit-box-orient: vertical; white-space: normal; font-size: 0.9em;
        }
        .hover-image img { max-width: 80px; max-height: 80px; display: block; margin: auto; object-fit: contain; border-radius: 4px; }
        .action-button {
            background: transparent; border: 1px solid var(--border-color-primary); cursor: pointer;
            padding: 4px; border-radius: 4px; display: flex; align-items: center; justify-content: center; margin: auto;
        }
        .action-button:hover { background-color: var(--background-fill-secondary); border-color: var(--primary-500); }
        .draggable-row { cursor: grab; transition: background-color 0.2s, border-top 0.1s; }
        .draggable-row:active { cursor: grabbing; }
        .draggable-row.drag-over { border-top: 3px solid #3b82f6 !important; background-color: rgba(59, 130, 246, 0.1) !important; }
        .draggable-row.drag-active { opacity: 0.5; background-color: var(--background-fill-secondary); }
        .alternating-grey-row:nth-child(even) { background-color: var(--table-even-background-fill); }
        .alternating-grey-row:nth-child(odd) { background-color: transparent; }
        
        .selected-row { background-color: rgba(59, 130, 246, 0.2) !important; border-left: 4px solid #3b82f6; }
        /* Override cursor for selection mode */
        .selection-active .draggable-row { cursor: pointer !important; }
        .selection-active .draggable-row:hover { background-color: rgba(59, 130, 246, 0.1) !important; }
        .replacements-list { margin-top: 10px; border: 1px solid var(--border-color-primary); padding: 10px; border-radius: 4px; background: var(--background-fill-secondary); }
        .replacements-list div { padding: 4px 0; border-bottom: 1px solid var(--border-color-primary); }
        .replacements-list div:last-child { border-bottom: none; }
        """
        
        with gr.Blocks() as demo:
            self.queue_state = gr.State([])
            self.qm_editing_index = gr.State(-1) 
            self.qm_mode = gr.State(False)
            self.qm_template_selection_mode = gr.State(False)
            self.qm_selected_template_idx = gr.State(-1)
            self.bulk_replace_state = gr.State([])
            
            gr.HTML(f"<style>{css}</style>")

            with gr.Row():
                with gr.Column(scale=1):
                    gr.Markdown("### Queue File Operations")
                    self.upload_btn = gr.UploadButton("Load queue.zip / .json", file_types=[".zip", ".json"], variant="primary")
                    self.download_btn = gr.DownloadButton("Save queue.zip", visible=False)
                    self.clear_btn = gr.Button("Clear Current List", variant="stop")
                    
                    self.bridge_btn = gr.Button("Bridge Images / Videos", variant="secondary")

                    with gr.Group(visible=False) as self.batch_group:
                        gr.Markdown("#### Bridge Operations")
                        self.batch_info = gr.Markdown("Please click a task row to select it as template.")
                        self.batch_files = gr.File(file_count="multiple", label="Input Files", visible=False)
                        with gr.Row(visible=False) as self.batch_options_row:
                            self.batch_mode = gr.Radio(["Append to Queue", "Replace Queue"], label="Action", value="Append to Queue")
                        
                        with gr.Row():
                            self.batch_btn = gr.Button("Generate", variant="primary", visible=False)
                            self.batch_cancel_btn = gr.Button("Cancel Bridge Operation", variant="stop")

                    self.bulk_replace_btn = gr.Button("Bulk Replace Loras", variant="secondary")

                    with gr.Group(visible=False) as self.bulk_group:
                        gr.Markdown("#### Bulk Lora Replacement")
                        with gr.Row(variant="compact"):
                            self.find_lora = gr.Dropdown(label="Replace", allow_custom_value=True, scale=3)
                            self.replace_lora = gr.Dropdown(label="With", allow_custom_value=True, scale=3)
                            self.add_replace_pair_btn = gr.Button("+", scale=0, min_width=40)
                        
                        self.pending_replacements_display = gr.HTML(
                            value="<div style='color:grey; font-style:italic;'>No pending replacements.</div>",
                            elem_classes="replacements-list"
                        )
                        
                        with gr.Row():
                            self.do_replace_btn = gr.Button("Replace All", variant="primary")
                            self.cancel_replace_btn = gr.Button("Cancel", variant="stop")

                    gr.Markdown("---")
                    gr.Markdown("**Instructions:**\n1. Load a `queue.zip`.\n2. Edit items (opens Main Edit Tab).\n3. 'Apply Edits' updates this list.\n4. Save modified zip.")

                with gr.Column(scale=3):
                    gr.Markdown("### Tasks")
                    self.queue_display = gr.HTML(value="<div style='padding:20px; text-align:center; color:grey;'>No queue loaded. Upload a file to begin.</div>")

            self.action_input = gr.Textbox(elem_id="qm_action_input", visible=False)
            self.action_trigger = gr.Button(elem_id="qm_action_trigger", visible=False)
            self.zip_output_file = gr.File(visible=False)

            self.upload_btn.upload(
                fn=self.load_queue_file,
                inputs=[self.upload_btn, self.state],
                outputs=[self.queue_state, self.queue_display, self.download_btn]
            )

            self.action_trigger.click(
                fn=self.handle_js_action,
                inputs=[self.action_input, self.queue_state, self.state, self.qm_template_selection_mode],
                outputs=[
                    self.queue_state, 
                    self.queue_display, 
                    self.queue_action_input, 
                    self.qm_editing_index, 
                    self.qm_mode,
                    self.qm_selected_template_idx,
                    self.qm_template_selection_mode,
                    self.batch_info,
                    self.batch_files,
                    self.batch_options_row,
                    self.batch_btn
                ]
            ).then(
                fn=None,
                js="(val) => { if (val && val.startsWith('edit_')) { document.getElementById('queue_action_trigger').click(); } }",
                inputs=[self.queue_action_input]
            )
            
            self.clear_btn.click(
                fn=lambda: ([], "<div style='padding:20px; text-align:center; color:grey;'>List cleared.</div>", gr.update(visible=False)),
                outputs=[self.queue_state, self.queue_display, self.download_btn]
            )
            
            self.download_btn.click(
                fn=self.save_current_queue,
                inputs=[self.queue_state],
                outputs=[self.zip_output_file]
            )
            
            self.zip_output_file.change(
                fn=None,
                js="(val) => { if (val) { const a = document.createElement('a'); a.href = val.url; a.download = val.orig_name; a.click(); } }",
                inputs=[self.zip_output_file]
            )

            self.bridge_btn.click(
                fn=self.toggle_template_selection,
                inputs=[self.queue_state],
                outputs=[self.qm_template_selection_mode, self.queue_display, self.bridge_btn, self.batch_group, self.batch_info, self.batch_files, self.batch_options_row, self.batch_btn, self.bulk_replace_btn]
            )
            
            self.batch_cancel_btn.click(
                fn=self.cancel_batch_operation,
                inputs=[self.queue_state],
                outputs=[self.qm_template_selection_mode, self.queue_display, self.bridge_btn, self.batch_group, self.bulk_replace_btn]
            )

            self.batch_btn.click(
                fn=self.process_batch_files,
                inputs=[self.batch_files, self.qm_selected_template_idx, self.batch_mode, self.queue_state],
                outputs=[self.queue_state, self.queue_display, self.download_btn, self.batch_group, self.bridge_btn, self.qm_template_selection_mode, self.bulk_replace_btn]
            )

            self.bulk_replace_btn.click(
                fn=self.open_bulk_replacer,
                inputs=[self.queue_state],
                outputs=[self.bulk_group, self.find_lora, self.replace_lora, self.bulk_replace_state, self.pending_replacements_display, self.bridge_btn, self.bulk_replace_btn]
            )

            self.add_replace_pair_btn.click(
                fn=self.add_replacement_pair,
                inputs=[self.find_lora, self.replace_lora, self.bulk_replace_state],
                outputs=[self.bulk_replace_state, self.pending_replacements_display, self.find_lora, self.replace_lora]
            )

            self.do_replace_btn.click(
                fn=self.perform_bulk_replace,
                inputs=[self.queue_state, self.bulk_replace_state],
                outputs=[self.queue_state, self.queue_display, self.bulk_group, self.bridge_btn, self.bulk_replace_btn]
            )

            self.cancel_replace_btn.click(
                fn=self.close_bulk_replacer,
                inputs=[],
                outputs=[self.bulk_group, self.bridge_btn, self.bulk_replace_btn]
            )

            self._wire_qm_logic()

        return demo

    def _get_available_loras(self, model_type):
        if not self.get_lora_dir or not model_type:
            return []
        
        try:
            lora_dir = self.get_lora_dir(model_type)
            if not lora_dir or not os.path.exists(lora_dir):
                return []
            
            files = []
            for ext in ['*.safetensors', '*.sft']:
                files.extend(glob.glob(os.path.join(lora_dir, ext)))
            
            choices = [os.path.basename(f) for f in files]
            choices.sort()
            return choices
        except Exception as e:
            print(f"Error fetching LoRAs: {e}")
            return []

    def _get_used_loras(self, queue):
        used = set()
        for task in queue:
            params = task.get('params', {})
            loras = params.get('activated_loras', [])
            if loras:
                for lora in loras:
                    used.add(os.path.basename(lora))
        
        sorted_used = sorted(list(used))
        return sorted_used

    def open_bulk_replacer(self, queue):
        if not queue:
            gr.Warning("Queue is empty.")
            return gr.update(), gr.update(), gr.update(), [], gr.update(), gr.update(), gr.update()

        model_type = None
        for task in queue:
            params = task.get('params', {})
            if params.get('model_type'):
                model_type = params.get('model_type')
                break

        used_loras = self._get_used_loras(queue)

        available_loras = self._get_available_loras(model_type)
        
        return (
            gr.update(visible=True),
            gr.update(choices=used_loras, value=None),
            gr.update(choices=available_loras, value=None),
            [],
            "<div style='color:grey; font-style:italic;'>No pending replacements.</div>",
            gr.update(visible=False),
            gr.update(visible=False)
        )

    def close_bulk_replacer(self):
        return gr.update(visible=False), gr.update(visible=True), gr.update(visible=True)

    def _render_replacements_list(self, replacements):
        if not replacements:
            return "<div style='color:grey; font-style:italic;'>No pending replacements.</div>"
        
        html_out = "<div class='replacements-list'>"
        html_out += "<div><strong>Pending Replacements:</strong></div>"
        for pair in replacements:
            html_out += f"<div>Replace <code>{pair['find']}</code> &rarr; <code>{pair['replace']}</code></div>"
        html_out += "</div>"
        return html_out

    def add_replacement_pair(self, find_val, replace_val, current_list):
        if not find_val or not replace_val:
            gr.Warning("Please select both a LoRA to find and a LoRA to replace with.")
            return current_list, gr.update(), gr.update(), gr.update()
        
        new_list = current_list + [{"find": find_val, "replace": replace_val}]
        html_out = self._render_replacements_list(new_list)
        
        return new_list, html_out, gr.update(value=None), gr.update(value=None)

    def perform_bulk_replace(self, queue, replacements):
        if not queue:
            return queue, gr.update(), gr.update(), gr.update(), gr.update()
        
        if not replacements:
            gr.Info("No replacements specified.")
            return queue, gr.update(), gr.update(), gr.update(), gr.update()

        updated_count = 0
        
        for task in queue:
            params = task.get('params', {})
            activated_loras = params.get('activated_loras', [])
            
            if not activated_loras:
                continue
                
            model_type = params.get('model_type')
            lora_dir = self.get_lora_dir(model_type) if model_type and self.get_lora_dir else ""
            
            new_activated_loras = []
            modified = False
            
            for lora_path in activated_loras:
                current_base = os.path.basename(lora_path)
                replaced = False
                
                for pair in replacements:
                    find_base = os.path.basename(pair['find'])
                    replace_base = os.path.basename(pair['replace'])
                    
                    if current_base == find_base:
                        if lora_dir:
                            new_path = os.path.join(lora_dir, replace_base)
                        else:
                            dir_name = os.path.dirname(lora_path)
                            new_path = os.path.join(dir_name, replace_base)
                        
                        new_activated_loras.append(new_path)
                        replaced = True
                        modified = True
                        break
                
                if not replaced:
                    new_activated_loras.append(lora_path)
            
            if modified:
                params['activated_loras'] = new_activated_loras
                updated_count += 1

        if updated_count > 0:
            gr.Info(f"Applied replacements to {updated_count} task(s).")
        else:
            gr.Info("No matching LoRAs found in queue.")

        html_update = self.generate_table_html(queue)
        
        return queue, html_update, gr.update(visible=False), gr.update(visible=True), gr.update(visible=True)

    def toggle_template_selection(self, queue):
        if not queue:
            gr.Warning("Queue is empty. Load a queue first.")
            return False, gr.update(), gr.update(), gr.update(visible=False), gr.update(), gr.update(), gr.update(), gr.update(), gr.update()
        gr.Info("Click a task row in the table to select it as the template.")
        html_update = self.generate_table_html(queue, selection_mode=True)
        help_text = "This feature allows you to bridge a sequence of images or videos by generating transitions between them.\n\n**Step 1:** Select a task below to use as a **template** for parameters (Prompt, LoRA, etc.).\n**Step 2:** Select the files you want to bridge."
        return True, html_update, gr.update(visible=False), gr.update(visible=True), help_text, gr.update(visible=False), gr.update(visible=False), gr.update(visible=False), gr.update(visible=False)

    def cancel_batch_operation(self, queue):
        html_update = self.generate_table_html(queue)
        return False, html_update, gr.update(visible=True), gr.update(visible=False), gr.update(visible=True)

    def _get_frame_from_file(self, file_path, position="start"):
        if not os.path.exists(file_path):
            return None

        if self.has_image_file_extension(file_path):
            try:
                return Image.open(file_path)
            except Exception as e:
                print(f"Error opening image {file_path}: {e}")
                return None

        if self.has_video_file_extension(file_path):
            try:
                with tempfile.TemporaryDirectory() as tmpdir:
                    extracted = self.extract_source_images(file_path, tmpdir)
                    if extracted:
                        target_key = 'image_end' if position == 'end' else 'image_start'
                        candidates = extracted.get(target_key, [])
                        if not isinstance(candidates, list):
                            candidates = [candidates]
                        
                        if candidates and candidates[0]:
                            path = candidates[-1] if position == 'end' else candidates[0]
                            if os.path.exists(path):
                                return Image.open(path)
            except Exception as e:
                print(f"Warning: Failed to extract embedded images from {file_path}: {e}")

            try:
                fps, _, _, frames_count = self.get_video_info(file_path)
                frame_idx = frames_count - 1 if position == 'end' else 0
                return self.get_video_frame(file_path, frame_idx, return_PIL=True)
            except Exception as e:
                print(f"Error extracting frame from {file_path}: {e}")
                return None
        
        return None

    def alphanum_key(self, s):
        return [int(c) if c.isdigit() else c.lower() for c in re.split('([0-9]+)', s)]

    def _pil_to_base64(self, pil_image, format="jpeg", quality=70):
        with io.BytesIO() as buffer:
            pil_image.save(buffer, format=format, quality=quality)
            img_bytes = buffer.getvalue()
            encoded_string = base64.b64encode(img_bytes).decode("utf-8")
            return f"data:image/{format};base64,{encoded_string}"

    def process_batch_files(self, files, template_idx, mode, current_queue):
        if not files or len(files) < 2:
            gr.Warning("Need at least 2 files to create bridge tasks.")
            return current_queue, gr.update(), gr.update(), gr.update(), gr.update(), gr.update(), gr.update()

        if not current_queue:
            gr.Warning("Queue is empty. Load a queue first to serve as a template.")
            return current_queue, gr.update(), gr.update(), gr.update(), gr.update(), gr.update(), gr.update()

        try:
            idx = int(template_idx)
            if idx < 0: idx = len(current_queue) + idx
            if idx < 0 or idx >= len(current_queue):
                raise IndexError
            template_task = current_queue[idx]
        except:
            gr.Warning("Please select a valid template task first.")
            return current_queue, gr.update(), gr.update(), gr.update(), gr.update(), gr.update(), gr.update()

        file_paths = [f.name for f in files]
        file_paths.sort(key=lambda f: self.alphanum_key(os.path.basename(f)))
        
        new_tasks = []
        start_id = 1
        if mode == "Append to Queue" and current_queue:
            start_id = max([t.get('id', 0) for t in current_queue]) + 1

        for i in range(len(file_paths) - 1):
            file_a = file_paths[i]
            file_b = file_paths[i+1]
            
            img_start = self._get_frame_from_file(file_a, "end")
            img_end = self._get_frame_from_file(file_b, "start")
            
            if not img_start or not img_end:
                print(f"Skipping pair {os.path.basename(file_a)} -> {os.path.basename(file_b)}: Could not extract frames.")
                continue

            new_task = copy.deepcopy(template_task)
            new_task['id'] = start_id + i
            params = new_task['params']

            params['image_start'] = [img_start]
            params['image_end'] = [img_end]

            current_ipt = params.get('image_prompt_type', '') or ''
            if 'S' not in current_ipt: current_ipt += 'S'
            if 'E' not in current_ipt: current_ipt += 'E'
            params['image_prompt_type'] = current_ipt

            params['video_source'] = None
            params['image_prompt_type'] = params['image_prompt_type'].replace('V', '').replace('L', '')

            try:
                start_b64 = self._pil_to_base64(img_start, format="jpeg", quality=70)
                end_b64 = self._pil_to_base64(img_end, format="jpeg", quality=70)
                new_task['start_image_data_base64'] = [start_b64]
                new_task['end_image_data_base64'] = [end_b64]
                new_task['start_image_data'] = [img_start]
                new_task['end_image_data'] = [img_end]
                new_task['start_image_labels'] = [f"End of {os.path.basename(file_a)}"]
                new_task['end_image_labels'] = [f"Start of {os.path.basename(file_b)}"]
            except Exception as e:
                print(f"Error creating thumbnails: {e}")

            new_tasks.append(new_task)

        if not new_tasks:
            gr.Warning("No valid tasks could be generated.")
            return current_queue, gr.update(), gr.update(), gr.update(), gr.update(), gr.update(), gr.update()

        if mode == "Replace Queue":
            final_queue = new_tasks
        else:
            final_queue = current_queue + new_tasks

        html_update = self.generate_table_html(final_queue)
        gr.Info(f"Generated {len(new_tasks)} bridge tasks.")

        return final_queue, html_update, gr.update(visible=True), gr.update(visible=False), gr.update(visible=True), False, gr.update(visible=True)

    def _wire_qm_logic(self):
        def toggle_buttons(is_qm_mode):
            return (gr.update(visible=is_qm_mode), gr.update(visible=not is_qm_mode), gr.update(visible=not is_qm_mode))

        self.qm_mode.change(
            fn=toggle_buttons,
            inputs=[self.qm_mode],
            outputs=[self.qm_buttons_row, self.main_edit_btn, self.main_cancel_btn]
        )

        if self.js_trigger_index:
            self.js_trigger_index.change(
                fn=self.post_apply_handler,
                inputs=[self.main_state, self.queue_state, self.qm_editing_index],
                outputs=[
                    self.main_tabs, 
                    self.queue_state, 
                    self.queue_display, 
                    self.qm_editing_index, 
                    self.live_queue_html, 
                    self.qm_mode
                ]
            )

        if self.qm_cancel_btn:
            self.qm_cancel_btn.click(
                fn=self.cleanup_temp_task,
                inputs=[self.main_state],
                outputs=[self.qm_editing_index, self.main_tabs, self.live_queue_html, self.qm_mode]
            )

    def cleanup_temp_task(self, state):
        gen = self.get_gen_info(state)
        live_queue = gen.get("queue", [])
        clean_queue = [t for t in live_queue if t.get('id', 0) >= -999]
        gen["queue"] = clean_queue
        self.update_queue_data(clean_queue)
        state["editing_task_id"] = None

        live_queue_html = self.update_queue_data(gen["queue"])
        return -1, gr.Tabs(selected="plugin_queue_manager_tab"), live_queue_html, False

    def load_queue_file(self, file_obj, state):
        if not file_obj:
            return [], "Error loading file.", gr.update(visible=False)
        filename = file_obj.name
        try:
            if filename.lower().endswith('.json'):
                with open(filename, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    if isinstance(data, dict): data = [data]
                    for i, task in enumerate(data):
                        if 'id' not in task: task['id'] = i + 100000 
                    queue_data = data
            else:
                queue_data, error = self._parse_queue_zip(filename, state)
                if error:
                    return [], f"Error parsing zip: {error}", gr.update(visible=False)
            html_table = self.generate_table_html(queue_data)
            return queue_data, html_table, gr.update(visible=True)
        except Exception as e:
            return [], f"Exception loading file: {str(e)}", gr.update(visible=False)

    def generate_table_html(self, queue, selected_index=-1, selection_mode=False):
        if not queue:
            return "<div style='padding:20px; text-align:center; color:grey;'>Queue is empty.</div>"
        
        wrapper_class = "qm-wrapper"
        if selection_mode:
            wrapper_class += " selection-active"

        html_str = f'<div class="{wrapper_class}">'
        html_str += """
        <table class="qm-table">
            <thead>
                <tr>
                    <th style="width:5%;" class="center-align">Qty</th>
                    <th style="width:auto;" class="text-left">Prompt</th>
                    <th style="width:7%;" class="center-align">Length</th>
                    <th style="width:7%;" class="center-align">Steps</th>
                    <th style="width:10%;" class="center-align">Start/Ref</th>
                    <th style="width:10%;" class="center-align">End</th>
                    <th style="width:4%;" class="center-align" title="Edit"></th>
                    <th style="width:4%;" class="center-align" title="Remove"></th>
                </tr>
            </thead>
            <tbody>
        """
        for i, task in enumerate(queue):
            params = task.get('params', {})
            full_prompt = str(task.get('prompt', params.get('prompt', '')))
            truncated_prompt = (html.escape(full_prompt[:97]) + '...') if len(full_prompt) > 100 else html.escape(full_prompt)
            start_data = task.get('start_image_data_base64')
            start_img_uri = start_data[0] if start_data and isinstance(start_data, list) and len(start_data) > 0 else None
            end_data = task.get('end_image_data_base64')
            end_img_uri = end_data[0] if end_data and isinstance(end_data, list) and len(end_data) > 0 else None
            start_img_div = f'<div class="hover-image"><img src="{start_img_uri}" alt="start"></div>' if start_img_uri else ""
            end_img_div = f'<div class="hover-image"><img src="{end_img_uri}" alt="end"></div>' if end_img_uri else ""
            steps = task.get('steps', params.get('num_inference_steps', '?'))
            length = task.get('length', params.get('video_length', '?'))
            
            edit_btn = f"""
            <button onclick="event.stopPropagation(); qmHandleAction('edit', {i})" class="action-button" title="Edit">
                <img src="/gradio_api/file=icons/edit.svg" style="width: 20px; height: 20px;" onerror="this.style.display='none';this.nextSibling.style.display='inline'">
                <span style="display:none; font-size:1.2em;">✏️</span>
            </button>"""
            remove_btn = f"""
            <button onclick="event.stopPropagation(); qmHandleAction('remove', {i})" class="action-button" title="Remove">
                <img src="/gradio_api/file=icons/remove.svg" style="width: 20px; height: 20px;" onerror="this.style.display='none';this.nextSibling.style.display='inline'">
                <span style="display:none; font-size:1.2em;">🗑️</span>
            </button>"""

            row_class = "draggable-row alternating-grey-row"
            if i == selected_index:
                row_class += " selected-row"

            row_html = f"""
            <tr draggable="true" class="{row_class}" data-index="{i}" 
                ondragstart="qmDragStart(event)" ondragover="qmDragOver(event)" ondrop="qmDrop(event)"
                ondragenter="qmDragEnter(event)" ondragleave="qmDragLeave(event)" ondragend="qmDragEnd(event)"
                onclick="qmRowClick(event, {i})"
                title="Drag to reorder / Click to select (if in selection mode)">
                <td class="center-align">{task.get('repeats', params.get('repeat_generation', 1))}</td>
                <td><div class="prompt-cell" title="{html.escape(full_prompt)}">{truncated_prompt}</div></td>
                <td class="center-align">{length}</td>
                <td class="center-align">{steps}</td>
                <td class="center-align">{start_img_div}</td>
                <td class="center-align">{end_img_div}</td>
                <td class="center-align">{edit_btn}</td>
                <td class="center-align">{remove_btn}</td>
            </tr>"""
            html_str += row_html
        html_str += "</tbody></table></div>"
        return html_str

    def handle_js_action(self, action_json, queue, state, selection_mode):
        updated_queue = queue
        html_update = gr.update()
        main_queue_input_update = gr.update()
        index_update = -1
        qm_mode_update = False
        selected_template_idx_update = gr.update()
        selection_mode_update = gr.update()
        batch_info_update = gr.update()
        batch_files_update = gr.update()
        batch_options_update = gr.update()
        batch_btn_update = gr.update()

        try:
            data = json.loads(action_json)
            action = data['action']
            param = data['param']
        except:
            return (updated_queue, html_update, main_queue_input_update, index_update, qm_mode_update, 
                    selected_template_idx_update, selection_mode_update, batch_info_update, batch_files_update, 
                    batch_options_update, batch_btn_update)

        if action == "select":
            index = int(param)
            if selection_mode:
                selected_template_idx_update = index
                selection_mode_update = False
                batch_info_update = f"Current Template: **Task #{index+1}**"
                batch_files_update = gr.update(visible=True)
                batch_options_update = gr.update(visible=True)
                batch_btn_update = gr.update(visible=True)
                html_update = self.generate_table_html(queue, index, selection_mode=False)
            else:
                pass

        elif action == "remove":
            index = int(param)
            if 0 <= index < len(queue):
                queue.pop(index)
                updated_queue = queue
                html_update = self.generate_table_html(updated_queue)
        
        elif action == "move":
            from_idx, to_idx = int(param[0]), int(param[1])
            if 0 <= from_idx < len(queue) and 0 <= to_idx < len(queue) and from_idx != to_idx:
                item = queue.pop(from_idx)
                queue.insert(to_idx, item)
                updated_queue = queue
                html_update = self.generate_table_html(updated_queue)
                
        elif action == "edit":
            index = int(param)
            if 0 <= index < len(queue):
                task = queue[index]
                gen = self.get_gen_info(state)
                live_queue = gen.get("queue", [])
                gen["queue"] = [t for t in live_queue if t.get('id', 0) >= -999]
                live_queue = gen["queue"] 
                temp_id = -1000 - index
                temp_task = copy.deepcopy(task)
                temp_task['id'] = temp_id
                live_queue.append(temp_task)
                self.update_queue_data(live_queue)

                index_update = index
                main_queue_input_update = f"edit_{temp_id}"
                qm_mode_update = True
                    
        return (updated_queue, html_update, main_queue_input_update, index_update, qm_mode_update, 
                selected_template_idx_update, selection_mode_update, batch_info_update, batch_files_update, 
                batch_options_update, batch_btn_update)

    def save_current_queue(self, queue):
        if not queue:
            gr.Warning("Queue is empty, nothing to save.")
            return None
        try:
            f = tempfile.NamedTemporaryFile(delete=False, suffix=".zip", prefix="modified_queue_")
            f.close()
            filename = f.name
            success = self._save_queue_to_zip(queue, filename)
            if success:
                return gr.File(value=filename, visible=False, label="queue.zip")
            else:
                gr.Error("Failed to save queue zip.")
                return None
        except Exception as e:
            print(f"Error saving queue: {e}")
            gr.Error(f"Error saving queue: {e}")
            return None